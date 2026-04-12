from __future__ import annotations

import math
import re
import uuid
from collections import Counter
from collections.abc import Sequence
from dataclasses import dataclass, field
from datetime import timedelta
from typing import Any

from sqlalchemy import delete, desc, func, or_, select, text
from sqlalchemy.ext.asyncio import AsyncSession

from app.ai import AiRuntime
from app.ai.schemas import (
    MemoryCommitPlan,
    MemoryPlanAction,
    MemoryPlanEntityDraft,
    MemoryPlanMemoryDraft,
    MemoryRecallBundle,
    MemoryRecallEntity,
    MemoryRecallHit,
    MemoryRecallRelation,
    MemorySemanticPayload,
    MemoryWriteDecision,
    MemoryNeighborhood,
    SavedMemoryDetail,
)
from app.core.logging import get_logger
from app.core.settings import RuntimeSettings
from app.models.admin import JobRun
from app.models.communication import Message
from app.models.enums import Direction, EntityRelationKind, MemoryEntityKind, MemoryFacet, MemoryRelationshipType, MemoryType
from app.models.memory import MemoryEntity, MemoryEntityRelation, MemoryItem, MemoryItemEntity, MemoryRelationship
from app.models.portal import ChildProfile
from app.models.persona import Persona
from app.schemas.site import (
    MemoryDeletePreview,
    MemoryDeletePreviewEntry,
    MemoryEntityView,
    MemoryGraphEdge,
    MemoryGraphNode,
    MemoryInspector,
    MemoryInspectorBreadcrumb,
    MemoryLinkedMemory,
    MemoryRecentChange,
)
from app.models.user import User
from app.services.prompt import PromptService
from app.utils.text import truncate_text
from app.utils.time import utc_now

logger = get_logger(__name__)

_DERIVED_RELATIONSHIP_TYPES = (
    MemoryRelationshipType.consolidated_into,
    MemoryRelationshipType.supersedes,
)
_CASCADE_RELATIONSHIP_TYPES = (
    MemoryRelationshipType.manual_child,
    MemoryRelationshipType.consolidated_into,
)


@dataclass(slots=True)
class RetrievedMemory:
    memory: MemoryItem
    score: float
    explanation: str


@dataclass(slots=True)
class MemoryGraphResult:
    nodes: list[MemoryGraphNode]
    structural_edges: list[MemoryGraphEdge]
    similarity_edges: list[MemoryGraphEdge]


@dataclass(slots=True)
class MemoryConceptAssignment:
    key: str | None
    label: str
    kind: str


@dataclass(slots=True)
class StructuredRelatedEntitySpec:
    display_name: str
    entity_kind: MemoryEntityKind
    relation_kind: EntityRelationKind = EntityRelationKind.related
    relation_to_child: str | None = None
    facet: MemoryFacet = MemoryFacet.identity
    canonical_value: str | None = None
    provenance_source: str | None = None
    role: str = "related"


@dataclass(slots=True)
class StructuredPlacement:
    primary_name: str | None
    primary_kind: MemoryEntityKind
    facet: MemoryFacet
    relation_to_child: str | None = None
    relation_kind: EntityRelationKind = EntityRelationKind.related
    canonical_value: str | None = None
    provenance_source: str | None = None
    role: str = "primary"
    confidence: float = 0.72
    related_entities: list[StructuredRelatedEntitySpec] = field(default_factory=list)


class MemoryService:
    def __init__(
        self,
        settings: RuntimeSettings,
        ai_runtime: AiRuntime,
        prompt_service: PromptService,
    ) -> None:
        self.settings = settings
        self.ai_runtime = ai_runtime
        self.prompt_service = prompt_service

    async def extract_from_message(
        self,
        session: AsyncSession,
        *,
        user: User,
        persona: Persona | None,
        message: Message,
        recent_messages: list[Message],
        config: dict[str, Any],
    ) -> list[MemoryItem]:
        if not message.body:
            return []
        decision = await self.plan_and_commit_text(
            session,
            user=user,
            persona=persona,
            latest_content=message.body,
            recent_messages=recent_messages,
            config=config,
            source_kind=str((message.metadata_json or {}).get("source_kind") or (message.metadata_json or {}).get("source") or "message"),
            source_channel=getattr(message.channel, "value", None) or str(message.channel or "message"),
            source_message=message,
            origin_key=str(message.idempotency_key or message.id),
            extra_metadata={
                "message_id": str(message.id),
                **dict(message.metadata_json or {}),
            },
        )
        if decision.status != "applied" or not decision.memory_ids:
            return []
        created_ids = [_normalize_uuid(item_id) for item_id in decision.memory_ids]
        valid_ids = [item_id for item_id in created_ids if item_id is not None]
        if not valid_ids:
            return []
        created_items = {
            item.id: item
            for item in (
                await session.execute(select(MemoryItem).where(MemoryItem.id.in_(valid_ids)))
            )
            .scalars()
            .all()
        }
        return [created_items[item_id] for item_id in valid_ids if item_id in created_items]

    async def embed_items(
        self,
        session: AsyncSession,
        items: list[MemoryItem],
        *,
        config: dict[str, Any],
    ) -> None:
        if not items or not self.ai_runtime.enabled:
            return
        texts = [self._embedding_text(item) for item in items]
        embeddings = await self.ai_runtime.embed_documents(texts)
        for item, embedding, text_value in zip(items, embeddings, texts):
            item.embedding_model = self.settings.openai.embedding_model
            item.embedding_text = text_value
            item.embedding_vector = embedding
        await session.flush()

    async def embed_pending_items(self, session: AsyncSession, *, config: dict[str, Any]) -> int:
        stmt = select(MemoryItem).where(MemoryItem.embedding_vector.is_(None)).limit(50)
        items = (await session.execute(stmt)).scalars().all()
        if not items:
            return 0
        await self.embed_items(session, list(items), config=config)
        return len(items)

    async def retrieve(
        self,
        session: AsyncSession,
        *,
        user_id,
        persona_id,
        query: str,
        top_k: int,
        threshold: float,
    ) -> list[RetrievedMemory]:
        normalized_user_id = _normalize_uuid(user_id)
        normalized_persona_id = _normalize_uuid(persona_id)
        if normalized_user_id is None:
            return []
        cleaned_query = " ".join(str(query or "").split()).strip()
        stmt = (
            select(MemoryItem)
            .where(
                MemoryItem.user_id == normalized_user_id,
                MemoryItem.disabled.is_(False),
            )
            .order_by(desc(MemoryItem.pinned), desc(MemoryItem.importance_score), desc(MemoryItem.created_at))
            .limit(max(top_k * 4, 24))
        )
        items = list((await session.execute(stmt)).scalars().all())
        if normalized_persona_id:
            items = [item for item in items if item.persona_id in (None, normalized_persona_id)]
        if not items:
            return []
        if not self.ai_runtime.enabled or not cleaned_query:
            fallback = [RetrievedMemory(memory=item, score=float(item.importance_score or 0.0), explanation="fallback_rank") for item in items[:top_k]]
            for item in fallback:
                item.memory.retrieval_count = int(item.memory.retrieval_count or 0) + 1
                item.memory.last_accessed_at = utc_now()
            await session.flush()
            return fallback
        embedding = await self.ai_runtime.embed_query(cleaned_query)
        if not embedding:
            return []
        dialect_name = session.bind.dialect.name if session.bind is not None else ""
        if dialect_name == "postgresql":
            results = await self._retrieve_postgres(
                session,
                user_id=normalized_user_id,
                persona_id=normalized_persona_id,
                query_embedding=embedding,
                top_k=top_k,
                threshold=threshold,
            )
        else:
            results = self._retrieve_python(items, embedding, top_k=top_k, threshold=threshold)
        results = self._apply_retrieval_penalties(results)
        for item in results:
            item.memory.retrieval_count = int(item.memory.retrieval_count or 0) + 1
            item.memory.last_accessed_at = utc_now()
        await session.flush()
        return results[:top_k]

    async def recall_bundle(
        self,
        session: AsyncSession,
        *,
        user_id,
        persona_id,
        query: str,
        top_k: int,
        threshold: float,
        recent_messages: Sequence[Message] | None = None,
        recent_snippets: Sequence[str] | None = None,
    ) -> MemoryRecallBundle:
        hits = await self.retrieve(
            session,
            user_id=user_id,
            persona_id=persona_id,
            query=query,
            top_k=top_k,
            threshold=threshold,
        )
        memory_ids = [item.memory.id for item in hits]
        memories_by_id: dict[uuid.UUID, MemoryItem] = {}
        if memory_ids:
            memories_by_id = {
                memory.id: memory
                for memory in (
                    await session.execute(select(MemoryItem).where(MemoryItem.id.in_(memory_ids)))
                )
                .scalars()
                .all()
            }
        memory_links: list[MemoryItemEntity] = []
        entities_by_id: dict[uuid.UUID, MemoryEntity] = {}
        relation_rows: list[MemoryEntityRelation] = []
        if memory_ids:
            memory_links = list(
                (
                    await session.execute(
                        select(MemoryItemEntity).where(MemoryItemEntity.memory_id.in_(memory_ids))
                    )
                )
                .scalars()
                .all()
            )
            entity_ids = {link.entity_id for link in memory_links}
            if entity_ids:
                entities_by_id = {
                    entity.id: entity
                    for entity in (
                        await session.execute(select(MemoryEntity).where(MemoryEntity.id.in_(list(entity_ids))))
                    )
                    .scalars()
                    .all()
                }
                relation_rows = list(
                    (
                        await session.execute(
                            select(MemoryEntityRelation).where(
                                or_(
                                    MemoryEntityRelation.parent_entity_id.in_(list(entity_ids)),
                                    MemoryEntityRelation.child_entity_id.in_(list(entity_ids)),
                                )
                            )
                        )
                    )
                    .scalars()
                    .all()
                )
        bundle_hits: list[MemoryRecallHit] = []
        for item in hits:
            memory = memories_by_id.get(item.memory.id, item.memory)
            attached_links = [link for link in memory_links if link.memory_id == memory.id]
            attached_entity_ids = {link.entity_id for link in attached_links}
            neighborhood_entities = [
                MemoryRecallEntity(
                    id=str(entity.id),
                    display_name=entity.display_name,
                    entity_kind=entity.entity_kind.value,
                    relation_to_child=entity.relation_to_child,
                    role=link.role,
                    semantic=self._semantic_payload_from_dict(entity.semantic_json),
                )
                for link in attached_links
                if (entity := entities_by_id.get(link.entity_id)) is not None
            ]
            neighborhood_relations = [
                MemoryRecallRelation(
                    source_id=str(row.parent_entity_id),
                    target_id=str(row.child_entity_id),
                    relationship_type=row.relationship_kind.value,
                    semantic=self._semantic_payload_from_dict(row.semantic_json),
                )
                for row in relation_rows
                if row.parent_entity_id in attached_entity_ids or row.child_entity_id in attached_entity_ids
            ]
            bundle_hits.append(
                MemoryRecallHit(
                    id=str(memory.id),
                    title=memory.title or "",
                    content=memory.content,
                    summary=memory.summary,
                    memory_type=memory.memory_type.value,
                    tags=list(memory.tags or []),
                    score=float(item.score),
                    explanation=item.explanation,
                    updated_at=memory.updated_at.isoformat() if memory.updated_at else None,
                    semantic=self._semantic_payload_from_memory(memory),
                    neighborhood=MemoryNeighborhood(
                        entities=neighborhood_entities,
                        relations=neighborhood_relations,
                        lineage=await self._memory_lineage_details(session, memory=memory),
                    ),
                )
            )
        normalized_recent_snippets = list(recent_snippets or [])
        if not normalized_recent_snippets and recent_messages:
            normalized_recent_snippets = self._recent_snippets_from_messages(recent_messages)
        return MemoryRecallBundle(
            query=" ".join(str(query or "").split()).strip(),
            recent_snippets=normalized_recent_snippets[:8],
            hits=bundle_hits,
        )

    async def plan_and_commit_text(
        self,
        session: AsyncSession,
        *,
        user: User,
        persona: Persona | None,
        latest_content: str,
        config: dict[str, Any],
        source_kind: str,
        source_channel: str,
        origin_key: str,
        recent_messages: Sequence[Message] | None = None,
        recent_snippets: Sequence[str] | None = None,
        source_message: Message | None = None,
        extra_metadata: dict[str, Any] | None = None,
        allow_follow_up: bool = True,
    ) -> MemoryWriteDecision:
        cleaned = truncate_text(" ".join(str(latest_content or "").split()), 4000)
        if not cleaned:
            return MemoryWriteDecision(status="skipped", summary="No content to evaluate.")
        if not self.ai_runtime.enabled:
            return MemoryWriteDecision(status="skipped", summary="AI runtime unavailable.")
        config_memory = dict(config.get("memory") or {})
        top_k = int(config_memory.get("top_k", self.settings.memory.top_k))
        threshold = float(config_memory.get("similarity_threshold", self.settings.memory.similarity_threshold))
        recall_bundle = await self.recall_bundle(
            session,
            user_id=user.id,
            persona_id=persona.id if persona else None,
            query=cleaned,
            top_k=max(min(top_k, 8), 3),
            threshold=threshold,
            recent_messages=recent_messages,
            recent_snippets=recent_snippets,
        )
        child_name = await self._resolve_child_name(session, user_id=user.id)
        planner_context = {
            "source_channel": source_channel,
            "source_kind": source_kind,
            "origin_key": origin_key,
            "child_name": child_name,
            "persona_name": getattr(persona, "display_name", None),
            "latest_content": cleaned,
            "recent_snippets": list(recall_bundle.recent_snippets),
            "recall_bundle": recall_bundle,
            "profile_context": self._memory_profile_context(user=user, persona=persona),
        }
        try:
            prompt = await self.prompt_service.render(session, "memory_commit_plan", planner_context)
            response = await self.ai_runtime.plan_memory_commit(prompt=prompt, max_tokens=1200, request_limit=6)
            plan = response.output
            if allow_follow_up and not plan.actions and (plan.follow_up_query or "").strip():
                recall_bundle = await self.recall_bundle(
                    session,
                    user_id=user.id,
                    persona_id=persona.id if persona else None,
                    query=plan.follow_up_query or cleaned,
                    top_k=max(min(top_k + 2, 10), 4),
                    threshold=threshold,
                    recent_messages=recent_messages,
                    recent_snippets=recent_snippets,
                )
                prompt = await self.prompt_service.render(
                    session,
                    "memory_commit_plan",
                    {
                        **planner_context,
                        "recall_bundle": recall_bundle,
                    },
                )
                response = await self.ai_runtime.plan_memory_commit(prompt=prompt, max_tokens=1200, request_limit=6)
                plan = response.output
        except Exception as exc:
            logger.warning(
                "memory_planner_failed",
                user_id=str(user.id),
                persona_id=str(persona.id) if persona else None,
                source_kind=source_kind,
                error=str(exc),
            )
            return MemoryWriteDecision(
                status="failed",
                summary="Planner failed closed.",
                recall_bundle=recall_bundle,
                error=str(exc),
            )
        if not plan.actions:
            return MemoryWriteDecision(
                status="skipped",
                summary=(plan.summary or "Planner chose not to write memory."),
                recall_bundle=recall_bundle,
            )
        decision = await self._apply_memory_commit_plan(
            session,
            user=user,
            persona=persona,
            plan=plan,
            config=config,
            source_kind=source_kind,
            source_channel=source_channel,
            origin_key=origin_key,
            source_message=source_message,
            source_model=response.model or self.settings.openai.chat_model,
            extra_metadata=extra_metadata or {},
            child_name=child_name,
        )
        decision.recall_bundle = recall_bundle
        return decision

    async def _memory_lineage_details(self, session: AsyncSession, *, memory: MemoryItem) -> list[SavedMemoryDetail]:
        related_ids: list[uuid.UUID] = []
        if memory.consolidated_into_id:
            related_ids.append(memory.consolidated_into_id)
        metadata = dict(memory.metadata_json or {})
        for key in ("supersedes_id", "superseded_by_id"):
            related_id = _normalize_uuid(metadata.get(key))
            if related_id is not None and related_id not in related_ids:
                related_ids.append(related_id)
        if not related_ids:
            return []
        related_items = {
            item.id: item
            for item in (
                await session.execute(select(MemoryItem).where(MemoryItem.id.in_(related_ids)))
            )
            .scalars()
            .all()
        }
        details: list[SavedMemoryDetail] = []
        for related_id in related_ids:
            item = related_items.get(related_id)
            if item is None:
                continue
            details.append(
                SavedMemoryDetail(
                    id=str(item.id),
                    title=_memory_display_title(item),
                    content=_memory_display_summary(item),
                    memory_type=item.memory_type.value,
                )
            )
        return details

    def _recent_snippets_from_messages(self, messages: Sequence[Message]) -> list[str]:
        snippets: list[str] = []
        for message in list(messages)[-8:]:
            body = " ".join(str(message.body or "").split()).strip()
            if not body:
                continue
            snippets.append(f"{getattr(message.direction, 'value', 'message')}: {truncate_text(body, 220)}")
        return snippets

    def _memory_profile_context(self, *, user: User, persona: Persona | None) -> str:
        profile_json = dict(getattr(user, "profile_json", {}) or {})
        lines = [
            f"User display name: {str(getattr(user, 'display_name', '') or '').strip() or 'Unknown'}",
            f"Persona name: {getattr(persona, 'display_name', None) or 'Resona'}",
            f"Persona description: {str(getattr(persona, 'description', '') or '').strip() or 'Not set'}",
            f"Persona style: {str(getattr(persona, 'style', '') or '').strip() or 'Not set'}",
        ]
        if profile_json:
            lines.append(f"Profile JSON: {profile_json}")
        return "\n".join(lines)

    async def _apply_memory_commit_plan(
        self,
        session: AsyncSession,
        *,
        user: User,
        persona: Persona | None,
        plan: MemoryCommitPlan,
        config: dict[str, Any],
        source_kind: str,
        source_channel: str,
        origin_key: str,
        source_message: Message | None,
        source_model: str,
        extra_metadata: dict[str, Any],
        child_name: str,
    ) -> MemoryWriteDecision:
        created_memories: list[MemoryItem] = []
        updated_memories: list[MemoryItem] = []
        created_entities: list[MemoryEntity] = []
        applied_actions: list[str] = []
        memory_refs: dict[str, MemoryItem] = {}
        entity_refs: dict[str, MemoryEntity] = {}
        root_entity = await self._ensure_child_root_entity(
            session,
            user_id=user.id,
            persona_id=persona.id if persona else None,
            child_name=child_name,
        )
        existing_origin_items = await self._recent_origin_memories(
            session,
            user_id=user.id,
            origin_key=origin_key,
        )
        existing_dedupe = {
            str((item.metadata_json or {}).get("planner_dedupe_key") or ""): item
            for item in existing_origin_items
            if str((item.metadata_json or {}).get("planner_dedupe_key") or "").strip()
        }

        for action in list(plan.actions or []):
            try:
                outcome = await self._apply_memory_plan_action(
                    session,
                    action=action,
                    user=user,
                    persona=persona,
                    root_entity=root_entity,
                    source_kind=source_kind,
                    source_channel=source_channel,
                    origin_key=origin_key,
                    source_message=source_message,
                    source_model=source_model,
                    extra_metadata=extra_metadata,
                    memory_refs=memory_refs,
                    entity_refs=entity_refs,
                    created_memories=created_memories,
                    updated_memories=updated_memories,
                    created_entities=created_entities,
                    existing_dedupe=existing_dedupe,
                )
            except Exception as exc:
                logger.warning(
                    "memory_plan_action_failed",
                    user_id=str(user.id),
                    action=action.action,
                    reason=action.reason,
                    error=str(exc),
                )
                continue
            if outcome:
                applied_actions.append(outcome)

        touched_memories = [*created_memories, *[item for item in updated_memories if item not in created_memories]]
        if touched_memories:
            await self.embed_items(session, touched_memories, config=config)
            await self.sync_relationships_for_user(session, user_id=user.id)
        details = [
            SavedMemoryDetail(
                id=str(item.id),
                title=_memory_display_title(item),
                content=_memory_display_summary(item),
                memory_type=item.memory_type.value,
            )
            for item in touched_memories
        ]
        return MemoryWriteDecision(
            status="applied" if touched_memories or created_entities else "skipped",
            summary=plan.summary or ("Applied memory plan." if touched_memories or created_entities else "Plan produced no writes."),
            applied_actions=applied_actions,
            memory_ids=[str(item.id) for item in touched_memories],
            entity_ids=[str(item.id) for item in created_entities],
            details=details,
        )

    async def _apply_memory_plan_action(
        self,
        session: AsyncSession,
        *,
        action: MemoryPlanAction,
        user: User,
        persona: Persona | None,
        root_entity: MemoryEntity,
        source_kind: str,
        source_channel: str,
        origin_key: str,
        source_message: Message | None,
        source_model: str,
        extra_metadata: dict[str, Any],
        memory_refs: dict[str, MemoryItem],
        entity_refs: dict[str, MemoryEntity],
        created_memories: list[MemoryItem],
        updated_memories: list[MemoryItem],
        created_entities: list[MemoryEntity],
        existing_dedupe: dict[str, MemoryItem],
    ) -> str | None:
        if action.action == "none":
            return None
        if action.action == "create_entity":
            entity = await self._ensure_plan_entity(
                session,
                draft=action.entity,
                user=user,
                persona=persona,
                root_entity=root_entity,
                source_kind=source_kind,
            )
            if entity is not None:
                if action.ref:
                    entity_refs[action.ref] = entity
                if action.entity and action.entity.ref:
                    entity_refs[action.entity.ref] = entity
                if entity not in created_entities:
                    created_entities.append(entity)
                return f"entity:{entity.display_name}"
            return None
        if action.action == "create_memory":
            memory = await self._create_planned_memory(
                session,
                draft=action.memory,
                explicit_entity_draft=action.entity,
                action=action,
                user=user,
                persona=persona,
                root_entity=root_entity,
                source_kind=source_kind,
                source_channel=source_channel,
                origin_key=origin_key,
                source_message=source_message,
                source_model=source_model,
                extra_metadata=extra_metadata,
                memory_refs=memory_refs,
                entity_refs=entity_refs,
                created_entities=created_entities,
                existing_dedupe=existing_dedupe,
            )
            if memory is not None:
                if action.ref:
                    memory_refs[action.ref] = memory
                if action.memory and action.memory.ref:
                    memory_refs[action.memory.ref] = memory
                parent_memory = await self._resolve_plan_memory(
                    session,
                    action.target_memory_id,
                    action.target_memory_ref,
                    user_id=user.id,
                    memory_refs=memory_refs,
                )
                if parent_memory is not None and parent_memory.id != memory.id:
                    await self._ensure_memory_relationship(
                        session,
                        user_id=user.id,
                        parent_memory_id=parent_memory.id,
                        child_memory_id=memory.id,
                        relationship_type=MemoryRelationshipType.manual_child,
                        metadata={"planner_origin_key": origin_key},
                    )
                if memory not in created_memories:
                    created_memories.append(memory)
                return f"memory:{_memory_display_title(memory)}"
            return None
        if action.action == "update_memory":
            memory = await self._resolve_plan_memory(
                session,
                action.target_memory_id or (action.memory.memory_id if action.memory else None),
                action.target_memory_ref or (action.memory.ref if action.memory else None),
                user_id=user.id,
                memory_refs=memory_refs,
            )
            if memory is None or action.memory is None:
                return None
            self._apply_memory_draft_to_item(
                memory,
                action.memory,
                source_kind=source_kind,
                source_channel=source_channel,
                origin_key=origin_key,
                source_model=source_model,
                extra_metadata=extra_metadata,
                source_message=source_message,
            )
            await self._rebuild_memory_attachments_from_plan(
                session,
                memory=memory,
                memory_draft=action.memory,
                explicit_entity_draft=action.entity,
                target_entity_ref=action.target_entity_ref,
                target_entity_id=action.target_entity_id,
                user=user,
                persona=persona,
                root_entity=root_entity,
                source_kind=source_kind,
                entity_refs=entity_refs,
                created_entities=created_entities,
            )
            if memory not in updated_memories:
                updated_memories.append(memory)
            return f"update:{_memory_display_title(memory)}"
        if action.action == "split_memory":
            split_parts = list(action.split_parts or [])
            for index, part in enumerate(split_parts, start=1):
                synthetic_action = MemoryPlanAction(
                    action="create_memory",
                    ref=f"{action.ref or 'split'}:{index}",
                    memory=part,
                    entity=action.entity,
                    target_entity_ref=action.target_entity_ref,
                    target_entity_id=action.target_entity_id,
                )
                outcome = await self._apply_memory_plan_action(
                    session,
                    action=synthetic_action,
                    user=user,
                    persona=persona,
                    root_entity=root_entity,
                    source_kind=source_kind,
                    source_channel=source_channel,
                    origin_key=origin_key,
                    source_message=source_message,
                    source_model=source_model,
                    extra_metadata=extra_metadata,
                    memory_refs=memory_refs,
                    entity_refs=entity_refs,
                    created_memories=created_memories,
                    updated_memories=updated_memories,
                    created_entities=created_entities,
                    existing_dedupe=existing_dedupe,
                )
                if outcome:
                    applied = outcome
            target = await self._resolve_plan_memory(
                session,
                action.target_memory_id,
                action.target_memory_ref,
                user_id=user.id,
                memory_refs=memory_refs,
            )
            if target is not None:
                target.disabled = True
                updated_memories.append(target)
            return f"split:{len(split_parts)}"
        if action.action == "attach_memory":
            attachment = action.attachment
            if attachment is None:
                return None
            memory = await self._resolve_plan_memory(
                session,
                attachment.memory_id or action.target_memory_id,
                attachment.memory_ref or action.target_memory_ref,
                user_id=user.id,
                memory_refs=memory_refs,
            )
            if memory is None:
                return None
            entity = await self._resolve_or_create_attachment_entity(
                session,
                attachment=attachment,
                action=action,
                user=user,
                persona=persona,
                root_entity=root_entity,
                source_kind=source_kind,
                entity_refs=entity_refs,
                created_entities=created_entities,
            )
            if entity is None:
                return None
            await self._attach_memory_entity(
                session,
                memory=memory,
                entity=entity,
                facet=_compat_memory_facet(attachment.facet_legacy or attachment.semantic.group),
                role=attachment.role or "primary",
                is_primary=bool(attachment.is_primary),
                semantic_json=(attachment.semantic.model_dump(mode="json") if attachment.semantic else {}),
                metadata={"planner_origin_key": origin_key},
            )
            if memory not in updated_memories:
                updated_memories.append(memory)
            return f"attach:{entity.display_name}"
        if action.action == "link_entities":
            relation = action.relation
            if relation is None:
                return None
            parent_entity = await self._resolve_plan_entity(
                session,
                relation.parent_id,
                relation.parent_ref,
                user_id=user.id,
                entity_refs=entity_refs,
            )
            child_entity = await self._resolve_plan_entity(
                session,
                relation.child_id,
                relation.child_ref,
                user_id=user.id,
                entity_refs=entity_refs,
            )
            if parent_entity is None or child_entity is None:
                return None
            await self._ensure_entity_relation(
                session,
                user_id=user.id,
                parent_entity_id=parent_entity.id,
                child_entity_id=child_entity.id,
                relationship_kind=_compat_relation_kind(relation.relationship_kind_legacy or relation.semantic.relation),
                semantic_json=(relation.semantic.model_dump(mode="json") if relation.semantic else {}),
                metadata={"planner_origin_key": origin_key},
            )
            return f"link:{parent_entity.display_name}->{child_entity.display_name}"
        if action.action in {"supersede_memory", "archive_memory"}:
            memory = await self._resolve_plan_memory(
                session,
                action.target_memory_id or (action.memory.memory_id if action.memory else None),
                action.target_memory_ref or (action.memory.ref if action.memory else None),
                user_id=user.id,
                memory_refs=memory_refs,
            )
            if memory is None:
                return None
            memory.disabled = True
            if action.action == "supersede_memory":
                metadata_json = dict(memory.metadata_json or {})
                metadata_json["superseded_at"] = utc_now().isoformat()
                memory.metadata_json = metadata_json
            if memory not in updated_memories:
                updated_memories.append(memory)
            return f"archive:{_memory_display_title(memory)}"
        return None

    async def _recent_origin_memories(
        self,
        session: AsyncSession,
        *,
        user_id: uuid.UUID,
        origin_key: str,
    ) -> list[MemoryItem]:
        if not origin_key:
            return []
        recent_items = list(
            (
                await session.execute(
                    select(MemoryItem)
                    .where(MemoryItem.user_id == user_id)
                    .order_by(desc(MemoryItem.created_at))
                    .limit(80)
                )
            )
            .scalars()
            .all()
        )
        return [
            item
            for item in recent_items
            if str((item.metadata_json or {}).get("planner_origin_key") or "") == origin_key
        ]

    async def _ensure_memory_relationship(
        self,
        session: AsyncSession,
        *,
        user_id: uuid.UUID,
        parent_memory_id: uuid.UUID,
        child_memory_id: uuid.UUID,
        relationship_type: MemoryRelationshipType,
        metadata: dict[str, Any] | None = None,
    ) -> None:
        if parent_memory_id == child_memory_id:
            return
        existing = await session.scalar(
            select(MemoryRelationship).where(
                MemoryRelationship.user_id == user_id,
                MemoryRelationship.parent_memory_id == parent_memory_id,
                MemoryRelationship.child_memory_id == child_memory_id,
                MemoryRelationship.relationship_type == relationship_type,
            )
        )
        if existing is not None:
            if metadata:
                existing.metadata_json = {**dict(existing.metadata_json or {}), **dict(metadata)}
                await session.flush()
            return
        session.add(
            MemoryRelationship(
                user_id=user_id,
                parent_memory_id=parent_memory_id,
                child_memory_id=child_memory_id,
                relationship_type=relationship_type,
                metadata_json=dict(metadata or {}),
            )
        )
        await session.flush()

    async def _resolve_plan_memory(
        self,
        session: AsyncSession,
        memory_id,
        memory_ref,
        *,
        user_id: uuid.UUID,
        memory_refs: dict[str, MemoryItem],
    ) -> MemoryItem | None:
        if memory_ref and memory_ref in memory_refs:
            return memory_refs[memory_ref]
        normalized_id = _normalize_uuid(memory_id)
        if normalized_id is None:
            return None
        return await session.scalar(
            select(MemoryItem).where(
                MemoryItem.id == normalized_id,
                MemoryItem.user_id == user_id,
            )
        )

    async def _resolve_plan_entity(
        self,
        session: AsyncSession,
        entity_id,
        entity_ref,
        *,
        user_id: uuid.UUID,
        entity_refs: dict[str, MemoryEntity],
    ) -> MemoryEntity | None:
        if entity_ref and entity_ref in entity_refs:
            return entity_refs[entity_ref]
        normalized_id = _normalize_uuid(entity_id)
        if normalized_id is None:
            return None
        return await session.scalar(
            select(MemoryEntity).where(
                MemoryEntity.id == normalized_id,
                MemoryEntity.user_id == user_id,
            )
        )

    async def _ensure_plan_entity(
        self,
        session: AsyncSession,
        *,
        draft: MemoryPlanEntityDraft | None,
        user: User,
        persona: Persona | None,
        root_entity: MemoryEntity,
        source_kind: str,
    ) -> MemoryEntity | None:
        if draft is None:
            return None
        display_name = " ".join(str(draft.display_name or "").split()).strip()
        if not display_name:
            return None
        entity = await self._upsert_memory_entity(
            session,
            user_id=user.id,
            persona_id=persona.id if persona else None,
            display_name=display_name,
            entity_kind=_compat_entity_kind(draft.entity_kind_legacy or draft.semantic.kind),
            facet=_compat_memory_facet(draft.default_facet_legacy or draft.semantic.group),
            relation_to_child=draft.relation_to_child,
            provenance_source=source_kind,
            canonical_value=draft.canonical_value,
            semantic_json=self._entity_semantic_dict(draft.semantic, label=display_name),
            metadata={"planner_source_kind": source_kind},
        )
        container_entity = await self._ensure_semantic_path_chain(
            session,
            user=user,
            persona=persona,
            root_entity=root_entity,
            semantic=draft.semantic,
            source_kind=source_kind,
        )
        if container_entity is not None and container_entity.id != entity.id:
            await self._ensure_entity_relation(
                session,
                user_id=user.id,
                parent_entity_id=container_entity.id,
                child_entity_id=entity.id,
                relationship_kind=_compat_relation_kind(draft.semantic.relation),
                semantic_json=self._relation_semantic_dict(draft.semantic),
                metadata={"planner_source_kind": source_kind},
            )
        return entity

    async def _ensure_semantic_path_chain(
        self,
        session: AsyncSession,
        *,
        user: User,
        persona: Persona | None,
        root_entity: MemoryEntity,
        semantic: MemorySemanticPayload | None,
        source_kind: str,
    ) -> MemoryEntity | None:
        if semantic is None:
            return None
        path_labels = [item for item in list(semantic.path or []) if item]
        if semantic.group and semantic.group not in path_labels:
            path_labels.insert(0, semantic.group)
        if not path_labels:
            return None
        current = root_entity
        previous = root_entity
        traversed: list[str] = []
        for label in path_labels:
            traversed.append(label)
            current = await self._upsert_memory_entity(
                session,
                user_id=user.id,
                persona_id=persona.id if persona else None,
                display_name=label,
                entity_kind=MemoryEntityKind.topic,
                facet=_compat_memory_facet(semantic.group),
                provenance_source=source_kind,
                canonical_value=None,
                semantic_json=self._path_semantic_dict(semantic, label=label, path=traversed),
                metadata={"planner_source_kind": source_kind},
            )
            if current.id != root_entity.id:
                await self._ensure_entity_relation(
                    session,
                    user_id=user.id,
                    parent_entity_id=root_entity.id if len(traversed) == 1 else previous.id,
                    child_entity_id=current.id,
                    relationship_kind=EntityRelationKind.related,
                    semantic_json=self._path_semantic_dict(semantic, label=label, path=traversed),
                    metadata={"planner_source_kind": source_kind},
                )
            previous = current
        return current

    async def _resolve_or_create_attachment_entity(
        self,
        session: AsyncSession,
        *,
        attachment,
        action: MemoryPlanAction,
        user: User,
        persona: Persona | None,
        root_entity: MemoryEntity,
        source_kind: str,
        entity_refs: dict[str, MemoryEntity],
        created_entities: list[MemoryEntity],
    ) -> MemoryEntity | None:
        entity = await self._resolve_plan_entity(
            session,
            attachment.entity_id or action.target_entity_id,
            attachment.entity_ref or action.target_entity_ref,
            user_id=user.id,
            entity_refs=entity_refs,
        )
        if entity is not None:
            return entity
        entity = await self._ensure_plan_entity(
            session,
            draft=action.entity,
            user=user,
            persona=persona,
            root_entity=root_entity,
            source_kind=source_kind,
        )
        if entity is not None and entity not in created_entities:
            created_entities.append(entity)
        return entity

    async def _create_planned_memory(
        self,
        session: AsyncSession,
        *,
        draft: MemoryPlanMemoryDraft | None,
        explicit_entity_draft: MemoryPlanEntityDraft | None,
        action: MemoryPlanAction,
        user: User,
        persona: Persona | None,
        root_entity: MemoryEntity,
        source_kind: str,
        source_channel: str,
        origin_key: str,
        source_message: Message | None,
        source_model: str,
        extra_metadata: dict[str, Any],
        memory_refs: dict[str, MemoryItem],
        entity_refs: dict[str, MemoryEntity],
        created_entities: list[MemoryEntity],
        existing_dedupe: dict[str, MemoryItem],
    ) -> MemoryItem | None:
        if draft is None:
            return None
        content = " ".join(str(draft.content or "").split()).strip()
        if not content:
            return None
        dedupe_key = normalize_text_fragment("|".join([draft.title or "", draft.summary or "", content]))
        if dedupe_key and dedupe_key in existing_dedupe:
            memory = existing_dedupe[dedupe_key]
            if draft.ref:
                memory_refs[draft.ref] = memory
            return memory
        item = MemoryItem(
            user_id=user.id,
            persona_id=persona.id if persona else None,
            source_message_id=source_message.id if source_message else None,
            memory_type=draft.memory_type,
            title=truncate_text(" ".join(str(draft.title or "").split()).strip(), 120) or None,
            content=content,
            summary=truncate_text(" ".join(str(draft.summary or "").split()).strip(), 300) or None,
            tags=list(draft.tags or []),
            importance_score=float(draft.importance_score or 0.5),
            metadata_json={},
        )
        self._apply_memory_draft_to_item(
            item,
            draft,
            source_kind=source_kind,
            source_channel=source_channel,
            origin_key=origin_key,
            source_model=source_model,
            extra_metadata=extra_metadata,
            source_message=source_message,
        )
        item.metadata_json["planner_dedupe_key"] = dedupe_key
        session.add(item)
        await session.flush()
        await self._rebuild_memory_attachments_from_plan(
            session,
            memory=item,
            memory_draft=draft,
            explicit_entity_draft=explicit_entity_draft,
            target_entity_ref=action.target_entity_ref,
            target_entity_id=action.target_entity_id,
            user=user,
            persona=persona,
            root_entity=root_entity,
            source_kind=source_kind,
            entity_refs=entity_refs,
            created_entities=created_entities,
        )
        existing_dedupe[dedupe_key] = item
        return item

    async def _rebuild_memory_attachments_from_plan(
        self,
        session: AsyncSession,
        *,
        memory: MemoryItem,
        memory_draft: MemoryPlanMemoryDraft,
        explicit_entity_draft: MemoryPlanEntityDraft | None,
        target_entity_ref: str | None,
        target_entity_id,
        user: User,
        persona: Persona | None,
        root_entity: MemoryEntity,
        source_kind: str,
        entity_refs: dict[str, MemoryEntity],
        created_entities: list[MemoryEntity],
    ) -> None:
        await session.execute(delete(MemoryItemEntity).where(MemoryItemEntity.memory_id == memory.id))
        explicit_entity = await self._resolve_plan_entity(
            session,
            target_entity_id,
            target_entity_ref or memory_draft.entity_ref,
            user_id=user.id,
            entity_refs=entity_refs,
        )
        if explicit_entity is None:
            explicit_entity = await self._ensure_plan_entity(
                session,
                draft=explicit_entity_draft,
                user=user,
                persona=persona,
                root_entity=root_entity,
                source_kind=source_kind,
            )
            if explicit_entity is not None and explicit_entity not in created_entities:
                created_entities.append(explicit_entity)
        semantic = memory_draft.semantic
        container_entity = await self._ensure_semantic_path_chain(
            session,
            user=user,
            persona=persona,
            root_entity=root_entity,
            semantic=semantic,
            source_kind=source_kind,
        )
        target_entity = explicit_entity or container_entity or root_entity
        if explicit_entity is not None and container_entity is not None and container_entity.id != explicit_entity.id:
            await self._ensure_entity_relation(
                session,
                user_id=user.id,
                parent_entity_id=container_entity.id,
                child_entity_id=explicit_entity.id,
                relationship_kind=_compat_relation_kind(semantic.relation),
                semantic_json=self._relation_semantic_dict(semantic),
                metadata={"planner_source_kind": source_kind},
            )
        await self._attach_memory_entity(
            session,
            memory=memory,
            entity=target_entity,
            facet=_compat_memory_facet(semantic.group),
            role="primary",
            is_primary=True,
            semantic_json=self._attachment_semantic_dict(semantic),
            metadata={"planner_source_kind": source_kind},
        )
        metadata_json = dict(memory.metadata_json or {})
        metadata_json["structured_primary_entity_id"] = str(target_entity.id)
        metadata_json["structured_primary_entity_name"] = target_entity.display_name
        metadata_json["structured_primary_entity_kind"] = target_entity.entity_kind.value
        metadata_json["entity_name"] = target_entity.display_name
        metadata_json["entity_kind"] = target_entity.entity_kind.value
        metadata_json["facet"] = _compat_memory_facet(semantic.group).value
        if target_entity.relation_to_child:
            metadata_json["relation_to_child"] = target_entity.relation_to_child
        if target_entity.canonical_value:
            metadata_json["canonical_value"] = target_entity.canonical_value
        memory.metadata_json = metadata_json

    def _apply_memory_draft_to_item(
        self,
        memory: MemoryItem,
        draft: MemoryPlanMemoryDraft,
        *,
        source_kind: str,
        source_channel: str,
        origin_key: str,
        source_model: str,
        extra_metadata: dict[str, Any],
        source_message: Message | None,
    ) -> None:
        semantic_dict = self._memory_semantic_dict(draft.semantic)
        metadata_json = {
            **dict(memory.metadata_json or {}),
            **dict(extra_metadata or {}),
            "source": source_kind,
            "source_kind": source_kind,
            "source_channel": source_channel,
            "planner_origin_key": origin_key,
            "source_model": source_model,
            "semantic": semantic_dict,
        }
        if source_message is not None:
            metadata_json["source_message_id"] = str(source_message.id)
        memory.memory_type = draft.memory_type
        memory.title = truncate_text(" ".join(str(draft.title or "").split()).strip(), 120) or None
        memory.content = " ".join(str(draft.content or "").split()).strip()
        memory.summary = truncate_text(" ".join(str(draft.summary or "").split()).strip(), 300) or None
        memory.tags = list(draft.tags or [])
        memory.importance_score = float(draft.importance_score or 0.5)
        memory.metadata_json = metadata_json

    def _memory_semantic_dict(self, semantic: MemorySemanticPayload | None) -> dict[str, Any]:
        if semantic is None:
            return {
                "world_section": "memories",
                "kind": "memory",
                "schema_version": 1,
            }
        return {
            **semantic.model_dump(mode="json"),
            "world_section": semantic.world_section or "memories",
            "kind": semantic.kind or "memory",
            "schema_version": int(semantic.schema_version or 1),
        }

    def _entity_semantic_dict(self, semantic: MemorySemanticPayload | None, *, label: str) -> dict[str, Any]:
        payload = self._memory_semantic_dict(semantic)
        payload["label"] = payload.get("label") or label
        payload["kind"] = payload.get("kind") or "entity"
        return payload

    def _path_semantic_dict(self, semantic: MemorySemanticPayload | None, *, label: str, path: list[str]) -> dict[str, Any]:
        payload = self._memory_semantic_dict(semantic)
        payload["label"] = label
        payload["kind"] = "group"
        payload["path"] = list(path)
        return payload

    def _attachment_semantic_dict(self, semantic: MemorySemanticPayload | None) -> dict[str, Any]:
        payload = self._memory_semantic_dict(semantic)
        payload["kind"] = payload.get("kind") or "attachment"
        return payload

    def _relation_semantic_dict(self, semantic: MemorySemanticPayload | None) -> dict[str, Any]:
        payload = self._memory_semantic_dict(semantic)
        payload["kind"] = payload.get("kind") or "relation"
        return payload

    def _semantic_payload_from_memory(self, memory: MemoryItem) -> MemorySemanticPayload | None:
        metadata = dict(memory.metadata_json or {})
        return self._semantic_payload_from_dict(metadata.get("semantic"))

    def _semantic_payload_from_dict(self, value: Any) -> MemorySemanticPayload | None:
        if not isinstance(value, dict) or not value:
            return None
        try:
            return MemorySemanticPayload.model_validate(value)
        except Exception:
            return None

    async def _retrieve_postgres(
        self,
        session: AsyncSession,
        *,
        user_id,
        persona_id,
        query_embedding: list[float],
        top_k: int,
        threshold: float,
    ) -> list[RetrievedMemory]:
        vector_literal = "[" + ",".join(f"{value:.12f}" for value in query_embedding) + "]"
        if persona_id is None:
            sql = text(
                """
                SELECT id, 1 - (embedding_vector <=> CAST(:vector_literal AS vector)) AS similarity
                FROM memory_items
                WHERE user_id = :user_id
                  AND disabled = false
                  AND embedding_vector IS NOT NULL
                ORDER BY embedding_vector <=> CAST(:vector_literal AS vector)
                LIMIT :top_k
                """
            )
            params = {"vector_literal": vector_literal, "user_id": user_id, "top_k": top_k}
        else:
            sql = text(
                """
                SELECT id, 1 - (embedding_vector <=> CAST(:vector_literal AS vector)) AS similarity
                FROM memory_items
                WHERE user_id = :user_id
                  AND disabled = false
                  AND embedding_vector IS NOT NULL
                  AND (persona_id IS NULL OR persona_id = :persona_id)
                ORDER BY embedding_vector <=> CAST(:vector_literal AS vector)
                LIMIT :top_k
                """
            )
            params = {"vector_literal": vector_literal, "user_id": user_id, "persona_id": persona_id, "top_k": top_k}
        rows = (await session.execute(sql, params)).all()
        if not rows:
            return []
        ids = [row.id for row in rows if row.similarity is None or row.similarity >= threshold]
        if not ids:
            return []
        stmt = select(MemoryItem).where(MemoryItem.id.in_(ids))
        by_id = {item.id: item for item in (await session.execute(stmt)).scalars().all()}
        results = []
        for row in rows:
            memory = by_id.get(row.id)
            if not memory:
                continue
            score = float(row.similarity or 0.0)
            if score < threshold:
                continue
            results.append(RetrievedMemory(memory=memory, score=score, explanation="pgvector_cosine"))
        return results

    async def _retrieve_schema_matches(
        self,
        session: AsyncSession,
        *,
        user_id,
        persona_id,
        query: str,
        top_k: int,
    ) -> list[RetrievedMemory]:
        normalized_query = normalize_text_fragment(query)
        if not normalized_query:
            return []
        entities = list(
            (
                await session.execute(
                    select(MemoryEntity).where(MemoryEntity.user_id == _normalize_uuid(user_id))
                )
            )
            .scalars()
            .all()
        )
        matches = [
            entity
            for entity in entities
            if entity.normalized_name and entity.normalized_name in normalized_query
        ]
        if not matches:
            return []
        entity_ids = [entity.id for entity in matches]
        links = list(
            (
                await session.execute(
                    select(MemoryItemEntity).where(MemoryItemEntity.entity_id.in_(entity_ids))
                )
            )
            .scalars()
            .all()
        )
        if not links:
            return []
        memory_ids = [link.memory_id for link in links]
        memories = {
            item.id: item
            for item in (
                await session.execute(
                    select(MemoryItem).where(
                        MemoryItem.id.in_(memory_ids),
                        MemoryItem.user_id == _normalize_uuid(user_id),
                        MemoryItem.disabled.is_(False),
                    )
                )
            )
            .scalars()
            .all()
        }
        entity_by_id = {entity.id: entity for entity in matches}
        results: list[RetrievedMemory] = []
        seen_ids: set[uuid.UUID] = set()
        for link in links:
            memory = memories.get(link.memory_id)
            if memory is None or memory.id in seen_ids:
                continue
            if persona_id and memory.persona_id not in (None, _normalize_uuid(persona_id)):
                continue
            entity = entity_by_id.get(link.entity_id)
            boost = 0.98 if bool(link.is_primary) else 0.88
            explanation = f"entity_match:{entity.display_name if entity else 'entity'}"
            results.append(RetrievedMemory(memory=memory, score=boost, explanation=explanation))
            seen_ids.add(memory.id)
        results.sort(key=lambda item: (item.memory.pinned, item.score, item.memory.importance_score), reverse=True)
        return results[:top_k]

    def _merge_retrieval_results(
        self,
        primary: list[RetrievedMemory],
        secondary: list[RetrievedMemory],
        *,
        top_k: int,
    ) -> list[RetrievedMemory]:
        merged: dict[uuid.UUID, RetrievedMemory] = {}
        for collection in (primary, secondary):
            for item in collection:
                existing = merged.get(item.memory.id)
                if existing is None or item.score > existing.score:
                    merged[item.memory.id] = item
        combined = list(merged.values())
        combined.sort(key=lambda item: (item.memory.pinned, item.score, item.memory.importance_score), reverse=True)
        return combined[:top_k]

    def _retrieve_python(
        self,
        items: list[MemoryItem],
        query_embedding: list[float],
        *,
        top_k: int,
        threshold: float,
    ) -> list[RetrievedMemory]:
        scored = []
        for item in items:
            if not _has_embedding(item.embedding_vector):
                continue
            score = cosine_similarity(item.embedding_vector, query_embedding)
            if score >= threshold:
                scored.append(RetrievedMemory(memory=item, score=score, explanation="python_cosine"))
        scored.sort(key=lambda item: (item.memory.pinned, item.score, item.memory.importance_score), reverse=True)
        return scored[:top_k]

    async def consolidate(self, session: AsyncSession, *, config: dict[str, Any]) -> int:
        target_messages = int(config["memory"]["summary_target_messages"])
        stmt = (
            select(Message)
            .where(Message.direction == Direction.inbound)
            .order_by(desc(Message.created_at))
            .limit(target_messages)
        )
        messages = list(reversed((await session.execute(stmt)).scalars().all()))
        if len(messages) < target_messages:
            return 0
        user_id = messages[-1].user_id
        persona_id = messages[-1].persona_id
        transcript = "\n".join(
            f"{item.direction.value}: {item.body or ''}" for item in messages if item.body
        )
        summary_text = transcript[:4000]
        if self.ai_runtime.enabled:
            fake_user = type("SummaryUser", (), {"id": user_id})()
            context = {"transcript": transcript, "config": config, "user": fake_user, "persona": None}
            rendered = await self.prompt_service.render(session, "summarization", context)
            try:
                response = await self.ai_runtime.consolidate_memory(
                    prompt=rendered,
                    max_tokens=self.settings.openai.memory_max_output_tokens,
                )
                summary_text = response.output.summary or summary_text
            except Exception:
                pass
        memory = MemoryItem(
            user_id=user_id,
            persona_id=persona_id,
            memory_type=MemoryType.summary,
            title="Conversation summary",
            content=summary_text,
            summary=summary_text[:300],
            importance_score=0.7,
            metadata_json={"source": "consolidation"},
        )
        session.add(memory)
        await session.flush()
        await self.embed_items(session, [memory], config=config)
        await self.sync_relationships_for_user(session, user_id=user_id)
        child_name = await self._resolve_child_name(session, user_id=user_id)
        await self.ensure_structure_for_memories(
            session,
            user_id=user_id,
            persona_id=persona_id,
            memories=[memory],
            child_name=child_name,
        )
        return 1

    async def ensure_structure_for_memories(
        self,
        session: AsyncSession,
        *,
        user_id: uuid.UUID | str,
        persona_id: uuid.UUID | str | None,
        memories: list[MemoryItem],
        child_name: str | None = None,
    ) -> None:
        normalized_user_id = _normalize_uuid(user_id)
        normalized_persona_id = _normalize_uuid(persona_id)
        if normalized_user_id is None or not memories:
            return
        resolved_child_name = child_name or await self._resolve_child_name(session, user_id=normalized_user_id)
        root_entity = await self._ensure_child_root_entity(
            session,
            user_id=normalized_user_id,
            persona_id=normalized_persona_id,
            child_name=resolved_child_name,
        )
        for memory in memories:
            await self._place_memory_into_structure(
                session,
                memory=memory,
                root_entity=root_entity,
                child_name=resolved_child_name,
            )
        await self._cleanup_orphan_entities(session, user_id=normalized_user_id, root_entity_id=root_entity.id)

    async def sync_entity_structure_for_user(
        self,
        session: AsyncSession,
        *,
        user_id: uuid.UUID | str,
        child_name: str | None = None,
        include_archived: bool = False,
    ) -> None:
        normalized_user_id = _normalize_uuid(user_id)
        if normalized_user_id is None:
            return
        stmt = select(MemoryItem).where(MemoryItem.user_id == normalized_user_id)
        if not include_archived:
            stmt = stmt.where(MemoryItem.disabled.is_(False))
        items = list((await session.execute(stmt.order_by(MemoryItem.created_at))).scalars().all())
        if not items:
            return
        item_ids = [item.id for item in items]
        linked_memory_ids = set(
            (
                await session.execute(
                    select(MemoryItemEntity.memory_id).where(MemoryItemEntity.memory_id.in_(item_ids))
                )
            )
            .scalars()
            .all()
        )
        legacy_items = [
            item
            for item in items
            if item.id not in linked_memory_ids and not isinstance((item.metadata_json or {}).get("semantic"), dict)
        ]
        if not legacy_items:
            return
        await self.ensure_structure_for_memories(
            session,
            user_id=normalized_user_id,
            persona_id=None,
            memories=legacy_items,
            child_name=child_name,
        )

    async def _resolve_child_name(self, session: AsyncSession, *, user_id: uuid.UUID) -> str:
        child_profile = await session.scalar(
            select(ChildProfile).where(ChildProfile.companion_user_id == user_id).order_by(ChildProfile.updated_at.desc())
        )
        if child_profile is not None:
            display = (child_profile.display_name or child_profile.first_name or "").strip()
            if display:
                return display
        user = await session.get(User, user_id)
        display = str(getattr(user, "display_name", "") or "").strip()
        if display:
            return display
        return "Child"

    async def _ensure_child_root_entity(
        self,
        session: AsyncSession,
        *,
        user_id: uuid.UUID,
        persona_id: uuid.UUID | None,
        child_name: str,
    ) -> MemoryEntity:
        existing = await session.scalar(
            select(MemoryEntity).where(
                MemoryEntity.user_id == user_id,
                MemoryEntity.entity_kind == MemoryEntityKind.child,
                MemoryEntity.is_primary.is_(True),
            )
        )
        normalized_name = child_name.casefold()
        if existing is not None:
            changed = False
            if existing.display_name != child_name:
                existing.display_name = child_name
                changed = True
            if existing.normalized_name != normalized_name:
                existing.normalized_name = normalized_name
                changed = True
            if persona_id is not None and existing.persona_id != persona_id:
                existing.persona_id = persona_id
                changed = True
            if existing.default_facet != MemoryFacet.identity:
                existing.default_facet = MemoryFacet.identity
                changed = True
            if existing.provenance_source != "child_profile":
                existing.provenance_source = "child_profile"
                changed = True
            desired_semantic = {
                "world_section": "memories",
                "kind": "child",
                "group": "child",
                "label": child_name,
                "path": [child_name],
                "confidence": 1.0,
                "schema_version": 1,
            }
            if dict(existing.semantic_json or {}) != desired_semantic:
                existing.semantic_json = desired_semantic
                changed = True
            if changed:
                await session.flush()
            return existing

        entity = MemoryEntity(
            user_id=user_id,
            persona_id=persona_id,
            display_name=child_name,
            normalized_name=normalized_name,
            entity_kind=MemoryEntityKind.child,
            default_facet=MemoryFacet.identity,
            provenance_source="child_profile",
            is_primary=True,
            semantic_json={
                "world_section": "memories",
                "kind": "child",
                "group": "child",
                "label": child_name,
                "path": [child_name],
                "confidence": 1.0,
                "schema_version": 1,
            },
            metadata_json={"schema_version": 1, "root": True},
        )
        session.add(entity)
        await session.flush()
        return entity

    async def _upsert_memory_entity(
        self,
        session: AsyncSession,
        *,
        user_id: uuid.UUID,
        persona_id: uuid.UUID | None,
        display_name: str,
        entity_kind: MemoryEntityKind,
        facet: MemoryFacet,
        relation_to_child: str | None = None,
        provenance_source: str | None = None,
        canonical_value: str | None = None,
        semantic_json: dict[str, Any] | None = None,
        metadata: dict[str, Any] | None = None,
    ) -> MemoryEntity:
        normalized_name = display_name.casefold()
        existing = await session.scalar(
            select(MemoryEntity).where(
                MemoryEntity.user_id == user_id,
                MemoryEntity.normalized_name == normalized_name,
                MemoryEntity.entity_kind == entity_kind,
            )
        )
        if existing is not None:
            existing.display_name = display_name
            existing.default_facet = facet
            if relation_to_child:
                existing.relation_to_child = relation_to_child
            if provenance_source:
                existing.provenance_source = provenance_source
            if canonical_value:
                existing.canonical_value = canonical_value
            if persona_id is not None and existing.persona_id is None:
                existing.persona_id = persona_id
            if semantic_json:
                existing.semantic_json = {**dict(existing.semantic_json or {}), **dict(semantic_json)}
            existing.metadata_json = {
                **dict(existing.metadata_json or {}),
                **dict(metadata or {}),
                "schema_version": 1,
            }
            await session.flush()
            return existing
        entity = MemoryEntity(
            user_id=user_id,
            persona_id=persona_id,
            display_name=display_name,
            normalized_name=normalized_name,
            entity_kind=entity_kind,
            default_facet=facet,
            relation_to_child=relation_to_child,
            provenance_source=provenance_source,
            canonical_value=canonical_value,
            semantic_json=dict(semantic_json or {}),
            metadata_json={"schema_version": 1, **dict(metadata or {})},
        )
        session.add(entity)
        await session.flush()
        return entity

    async def _ensure_entity_relation(
        self,
        session: AsyncSession,
        *,
        user_id: uuid.UUID,
        parent_entity_id: uuid.UUID,
        child_entity_id: uuid.UUID,
        relationship_kind: EntityRelationKind,
        semantic_json: dict[str, Any] | None = None,
        metadata: dict[str, Any] | None = None,
    ) -> None:
        if parent_entity_id == child_entity_id:
            return
        existing = await session.scalar(
            select(MemoryEntityRelation).where(
                MemoryEntityRelation.user_id == user_id,
                MemoryEntityRelation.parent_entity_id == parent_entity_id,
                MemoryEntityRelation.child_entity_id == child_entity_id,
                MemoryEntityRelation.relationship_kind == relationship_kind,
            )
        )
        if existing is not None:
            if semantic_json:
                existing.semantic_json = {**dict(existing.semantic_json or {}), **dict(semantic_json)}
            if metadata:
                existing.metadata_json = {**dict(existing.metadata_json or {}), **dict(metadata)}
                await session.flush()
            return
        session.add(
            MemoryEntityRelation(
                user_id=user_id,
                parent_entity_id=parent_entity_id,
                child_entity_id=child_entity_id,
                relationship_kind=relationship_kind,
                semantic_json=dict(semantic_json or {}),
                metadata_json=dict(metadata or {}),
            )
        )
        await session.flush()

    async def _attach_memory_entity(
        self,
        session: AsyncSession,
        *,
        memory: MemoryItem,
        entity: MemoryEntity,
        facet: MemoryFacet,
        role: str,
        is_primary: bool,
        semantic_json: dict[str, Any] | None = None,
        metadata: dict[str, Any] | None = None,
    ) -> None:
        existing = await session.scalar(
            select(MemoryItemEntity).where(
                MemoryItemEntity.memory_id == memory.id,
                MemoryItemEntity.entity_id == entity.id,
                MemoryItemEntity.role == role,
            )
        )
        if existing is not None:
            existing.facet = facet
            existing.is_primary = is_primary
            if semantic_json:
                existing.semantic_json = {**dict(existing.semantic_json or {}), **dict(semantic_json)}
            existing.metadata_json = {**dict(existing.metadata_json or {}), **dict(metadata or {})}
            await session.flush()
            return
        session.add(
            MemoryItemEntity(
                memory_id=memory.id,
                entity_id=entity.id,
                role=role,
                facet=facet,
                is_primary=is_primary,
                semantic_json=dict(semantic_json or {}),
                metadata_json=dict(metadata or {}),
            )
        )
        await session.flush()

    async def _place_memory_into_structure(
        self,
        session: AsyncSession,
        *,
        memory: MemoryItem,
        root_entity: MemoryEntity,
        child_name: str,
    ) -> None:
        placement = await self._determine_structured_placement(
            session,
            memory=memory,
            child_name=child_name,
            root_entity=root_entity,
        )
        await session.execute(delete(MemoryItemEntity).where(MemoryItemEntity.memory_id == memory.id))
        primary_entity = root_entity
        if placement.primary_name and placement.primary_name.casefold() != root_entity.normalized_name:
            primary_entity = await self._upsert_memory_entity(
                session,
                user_id=root_entity.user_id,
                persona_id=memory.persona_id,
                display_name=placement.primary_name,
                entity_kind=placement.primary_kind,
                facet=placement.facet,
                relation_to_child=placement.relation_to_child,
                provenance_source=placement.provenance_source or str((memory.metadata_json or {}).get("source") or ""),
                canonical_value=placement.canonical_value,
                metadata={"from_memory_id": str(memory.id)},
            )
            await self._ensure_entity_relation(
                session,
                user_id=root_entity.user_id,
                parent_entity_id=root_entity.id,
                child_entity_id=primary_entity.id,
                relationship_kind=placement.relation_kind,
                metadata={"memory_id": str(memory.id)},
            )

        await self._attach_memory_entity(
            session,
            memory=memory,
            entity=primary_entity,
            facet=placement.facet,
            role=placement.role,
            is_primary=True,
            metadata={"schema_version": 1},
        )

        for related in placement.related_entities:
            related_entity = await self._upsert_memory_entity(
                session,
                user_id=root_entity.user_id,
                persona_id=memory.persona_id,
                display_name=related.display_name,
                entity_kind=related.entity_kind,
                facet=related.facet,
                relation_to_child=related.relation_to_child,
                provenance_source=related.provenance_source or str((memory.metadata_json or {}).get("source") or ""),
                canonical_value=related.canonical_value,
                metadata={"from_memory_id": str(memory.id)},
            )
            parent_entity = root_entity if primary_entity.id == root_entity.id else primary_entity
            await self._ensure_entity_relation(
                session,
                user_id=root_entity.user_id,
                parent_entity_id=parent_entity.id,
                child_entity_id=related_entity.id,
                relationship_kind=related.relation_kind,
                metadata={"memory_id": str(memory.id)},
            )
            await self._attach_memory_entity(
                session,
                memory=memory,
                entity=related_entity,
                facet=related.facet,
                role=related.role,
                is_primary=False,
                metadata={"schema_version": 1},
            )

        metadata_json = dict(memory.metadata_json or {})
        metadata_json.update(
            {
                "schema_version": 1,
                "facet": placement.facet.value,
                "relation_to_child": placement.relation_to_child,
                "provenance_source": placement.provenance_source or metadata_json.get("source"),
                "structured_primary_entity_id": str(primary_entity.id),
                "structured_primary_entity_name": primary_entity.display_name,
                "structured_primary_entity_kind": primary_entity.entity_kind.value,
            }
        )
        if primary_entity.id != root_entity.id:
            metadata_json["entity_name"] = primary_entity.display_name
            metadata_json["entity_kind"] = primary_entity.entity_kind.value
        memory.metadata_json = metadata_json
        await session.flush()

    async def _cleanup_orphan_entities(
        self,
        session: AsyncSession,
        *,
        user_id: uuid.UUID,
        root_entity_id: uuid.UUID,
    ) -> None:
        entities = list(
            (
                await session.execute(
                    select(MemoryEntity).where(
                        MemoryEntity.user_id == user_id,
                        MemoryEntity.id != root_entity_id,
                    )
                )
            )
            .scalars()
            .all()
        )
        if not entities:
            return
        attached_entity_ids = {
            row[0]
            for row in (
                await session.execute(
                    select(MemoryItemEntity.entity_id).join(MemoryItem, MemoryItem.id == MemoryItemEntity.memory_id).where(
                        MemoryItem.user_id == user_id
                    )
                )
            ).all()
        }
        connected_entity_ids = {
            row[0]
            for row in (
                await session.execute(
                    select(MemoryEntityRelation.parent_entity_id).where(MemoryEntityRelation.user_id == user_id)
                )
            ).all()
        } | {
            row[0]
            for row in (
                await session.execute(
                    select(MemoryEntityRelation.child_entity_id).where(MemoryEntityRelation.user_id == user_id)
                )
            ).all()
        }
        orphan_ids = [
            entity.id
            for entity in entities
            if entity.id not in attached_entity_ids and entity.id not in connected_entity_ids
        ]
        if not orphan_ids:
            return
        await session.execute(delete(MemoryEntity).where(MemoryEntity.id.in_(orphan_ids)))
        await session.flush()

    async def sync_relationships_for_user(self, session: AsyncSession, *, user_id: uuid.UUID | str) -> None:
        normalized_user_id = _normalize_uuid(user_id)
        if normalized_user_id is None:
            return

        items = list(
            (
                await session.execute(
                    select(MemoryItem.id, MemoryItem.consolidated_into_id, MemoryItem.metadata_json).where(
                        MemoryItem.user_id == normalized_user_id
                    )
                )
            ).all()
        )
        known_ids = {row.id for row in items}
        desired: list[tuple[uuid.UUID, uuid.UUID, MemoryRelationshipType]] = []
        seen: set[tuple[uuid.UUID, uuid.UUID, MemoryRelationshipType]] = set()
        for row in items:
            if row.consolidated_into_id and row.consolidated_into_id in known_ids and row.consolidated_into_id != row.id:
                edge = (row.consolidated_into_id, row.id, MemoryRelationshipType.consolidated_into)
                if edge not in seen:
                    desired.append(edge)
                    seen.add(edge)
            supersedes_id = _normalize_uuid((row.metadata_json or {}).get("supersedes_id"))
            if supersedes_id and supersedes_id in known_ids and supersedes_id != row.id:
                edge = (supersedes_id, row.id, MemoryRelationshipType.supersedes)
                if edge not in seen:
                    desired.append(edge)
                    seen.add(edge)

        await session.execute(
            delete(MemoryRelationship).where(
                MemoryRelationship.user_id == normalized_user_id,
                MemoryRelationship.relationship_type.in_(_DERIVED_RELATIONSHIP_TYPES),
            )
        )
        for parent_id, child_id, relationship_type in desired:
            session.add(
                MemoryRelationship(
                    user_id=normalized_user_id,
                    parent_memory_id=parent_id,
                    child_memory_id=child_id,
                    relationship_type=relationship_type,
                )
            )
        await session.flush()

    async def _collapse_duplicate_memories(
        self,
        session: AsyncSession,
        *,
        user_id: uuid.UUID,
    ) -> list[dict[str, Any]]:
        memories = list(
            (
                await session.execute(
                    select(MemoryItem)
                    .where(
                        MemoryItem.user_id == user_id,
                        MemoryItem.disabled.is_(False),
                    )
                    .order_by(desc(MemoryItem.pinned), desc(MemoryItem.updated_at), desc(MemoryItem.created_at))
                )
            )
            .scalars()
            .all()
        )
        if len(memories) < 2:
            return []

        memory_ids = [memory.id for memory in memories]
        primary_links = list(
            (
                await session.execute(
                    select(MemoryItemEntity).where(
                        MemoryItemEntity.memory_id.in_(memory_ids),
                        MemoryItemEntity.is_primary.is_(True),
                    )
                )
            )
            .scalars()
            .all()
        )
        primary_entity_by_memory = {link.memory_id: link.entity_id for link in primary_links}

        grouped: dict[tuple[str | None, str, str, str, str], list[MemoryItem]] = {}
        for memory in memories:
            title_sig = normalize_text_fragment(memory.title or "")
            content_sig = normalize_text_fragment(memory.summary or memory.content or "")
            if not title_sig and not content_sig:
                continue
            group_key = (
                str(primary_entity_by_memory.get(memory.id)) if primary_entity_by_memory.get(memory.id) else None,
                self._memory_world_section(memory),
                memory.memory_type.value,
                title_sig,
                content_sig,
            )
            grouped.setdefault(group_key, []).append(memory)

        cleanup_at = utc_now().isoformat()
        changes: list[dict[str, Any]] = []
        for duplicate_group in grouped.values():
            if len(duplicate_group) < 2:
                continue
            ordered = sorted(
                duplicate_group,
                key=lambda item: (
                    bool(item.pinned),
                    float(item.importance_score or 0.0),
                    item.updated_at or item.created_at or utc_now(),
                ),
                reverse=True,
            )
            keeper = ordered[0]
            for duplicate in ordered[1:]:
                if duplicate.id == keeper.id or duplicate.disabled:
                    continue
                duplicate.disabled = True
                duplicate.consolidated_into_id = keeper.id
                duplicate.metadata_json = {
                    **dict(duplicate.metadata_json or {}),
                    "health_cleanup_at": cleanup_at,
                    "health_cleanup_reason": "duplicate_memory",
                    "health_cleanup_target_id": str(keeper.id),
                }
                changes.append(
                    {
                        "id": f"memory-health:{duplicate.id}",
                        "user_id": str(user_id),
                        "change_type": "cleanup",
                        "title": f"Merged duplicate memory into {_memory_display_title(keeper)}",
                        "summary": (
                            f"Archived duplicate memory “{_memory_display_title(duplicate)}” so this branch stays easier to browse."
                        ),
                        "occurred_at": cleanup_at,
                        "memory_id": str(keeper.id),
                        "node_id": str(keeper.id),
                        "href": f"/app/memories/map?node={keeper.id}",
                        "tone": "warning",
                    }
                )
        if changes:
            await session.flush()
            await self.sync_relationships_for_user(session, user_id=user_id)
        return changes

    async def list_memories_for_user(
        self,
        session: AsyncSession,
        *,
        user_id: uuid.UUID | str,
        include_archived: bool = False,
        search: str | None = None,
        memory_type: str | None = None,
        limit: int = 120,
    ) -> list[MemoryItem]:
        normalized_user_id = _normalize_uuid(user_id)
        if normalized_user_id is None:
            return []

        stmt = select(MemoryItem).where(MemoryItem.user_id == normalized_user_id)
        if not include_archived:
            stmt = stmt.where(MemoryItem.disabled.is_(False))
        normalized_search = str(search or "").strip()
        if normalized_search:
            search_term = f"%{normalized_search}%"
            stmt = stmt.where(
                or_(
                    MemoryItem.title.ilike(search_term),
                    MemoryItem.summary.ilike(search_term),
                    MemoryItem.content.ilike(search_term),
                )
            )
        normalized_type = str(memory_type or "").strip()
        if normalized_type:
            try:
                stmt = stmt.where(MemoryItem.memory_type == MemoryType(normalized_type))
            except ValueError:
                return []
        stmt = stmt.order_by(desc(MemoryItem.pinned), desc(MemoryItem.importance_score), desc(MemoryItem.updated_at)).limit(
            max(limit, 1)
        )
        return list((await session.execute(stmt)).scalars().all())

    async def list_recent_memories_for_user(
        self,
        session: AsyncSession,
        *,
        user_id: uuid.UUID | str,
        include_archived: bool = False,
        world_section: str | None = None,
        page: int = 1,
        page_size: int = 5,
    ) -> tuple[list[MemoryItem], int]:
        normalized_user_id = _normalize_uuid(user_id)
        if normalized_user_id is None:
            return [], 0

        stmt = (
            select(MemoryItem)
            .where(MemoryItem.user_id == normalized_user_id)
            .order_by(desc(MemoryItem.updated_at), desc(MemoryItem.created_at))
        )
        if not include_archived:
            stmt = stmt.where(MemoryItem.disabled.is_(False))

        memories = list((await session.execute(stmt)).scalars().all())
        normalized_world_section = str(world_section or "").strip()
        if normalized_world_section:
            memories = [
                memory
                for memory in memories
                if self._memory_world_section(memory) == normalized_world_section
            ]

        total = len(memories)
        safe_page = max(page, 1)
        safe_page_size = max(page_size, 1)
        start = (safe_page - 1) * safe_page_size
        end = start + safe_page_size
        return memories[start:end], total

    async def recent_changes(
        self,
        session: AsyncSession,
        *,
        user_id: uuid.UUID | str,
        limit: int = 14,
    ) -> list[MemoryRecentChange]:
        normalized_user_id = _normalize_uuid(user_id)
        if normalized_user_id is None:
            return []

        memory_rows = list(
            (
                await session.execute(
                    select(MemoryItem)
                    .where(MemoryItem.user_id == normalized_user_id)
                    .order_by(desc(MemoryItem.updated_at), desc(MemoryItem.created_at))
                    .limit(max(limit * 3, 30))
                )
            )
            .scalars()
            .all()
        )
        changes: list[MemoryRecentChange] = []
        for memory in memory_rows:
            metadata = dict(memory.metadata_json or {})
            if metadata.get("health_cleanup_at") and metadata.get("health_cleanup_reason"):
                continue
            occurred_at = memory.updated_at or memory.created_at
            if occurred_at is None:
                continue
            was_edited = (
                memory.created_at is not None
                and memory.updated_at is not None
                and abs((memory.updated_at - memory.created_at).total_seconds()) >= 2
            )
            if memory.disabled:
                change_type = "archived"
                tone = "warning"
            elif was_edited:
                change_type = "edited"
                tone = "info"
            else:
                change_type = "created"
                tone = "success"
            href_base = "/app/memories/daily-routine" if self._memory_world_section(memory) == "daily_routine" else "/app/memories/map"
            changes.append(
                MemoryRecentChange(
                    id=f"memory:{memory.id}:{change_type}",
                    change_type=change_type,
                    title=_memory_display_title(memory),
                    summary=_memory_display_summary(memory),
                    occurred_at=occurred_at.isoformat(),
                    memory_id=str(memory.id),
                    node_id=str(memory.id),
                    href=f"{href_base}?node={memory.id}",
                    tone=tone,
                    source_label=_memory_change_source_label(metadata),
                )
            )

        job_runs = list(
            (
                await session.execute(
                    select(JobRun)
                    .where(JobRun.job_name == "memory_health")
                    .order_by(desc(JobRun.created_at))
                    .limit(12)
                )
            )
            .scalars()
            .all()
        )
        for run in job_runs:
            details = dict(run.details_json or {})
            for item in list(details.get("changes") or []):
                if not isinstance(item, dict):
                    continue
                if str(item.get("user_id") or "") != str(normalized_user_id):
                    continue
                memory_id = str(item.get("memory_id") or "").strip() or None
                node_id = str(item.get("node_id") or memory_id or "").strip() or None
                href = str(item.get("href") or "").strip() or None
                if href is None and node_id:
                    href = f"/app/memories/map?node={node_id}"
                changes.append(
                    MemoryRecentChange(
                        id=str(item.get("id") or f"memory-health:{run.id}:{memory_id or node_id or len(changes)}"),
                        change_type=str(item.get("change_type") or "cleanup"),
                        title=str(item.get("title") or "Memory cleanup"),
                        summary=str(item.get("summary") or "").strip() or "Resona cleaned up the memory map automatically.",
                        occurred_at=str(item.get("occurred_at") or (run.finished_at or run.created_at).isoformat()),
                        memory_id=memory_id,
                        node_id=node_id,
                        href=href,
                        tone=str(item.get("tone") or "warning"),
                        source_label="Daily memory health",
                    )
                )

        unique: dict[str, MemoryRecentChange] = {}
        for change in changes:
            unique[change.id] = change
        ordered = sorted(
            unique.values(),
            key=lambda item: item.occurred_at or "",
            reverse=True,
        )
        return ordered[: max(limit, 1)]

    async def run_memory_health(self, session: AsyncSession, *, config: dict[str, Any]) -> dict[str, Any]:
        user_ids = [
            user_id
            for user_id in (
                await session.execute(
                    select(MemoryItem.user_id).where(MemoryItem.user_id.is_not(None)).distinct()
                )
            )
            .scalars()
            .all()
            if user_id is not None
        ]
        total_changes = 0
        changes: list[dict[str, Any]] = []
        users_processed = 0

        for user_id in user_ids:
            child_name = await self._resolve_child_name(session, user_id=user_id)
            await self.sync_entity_structure_for_user(
                session,
                user_id=user_id,
                child_name=child_name,
                include_archived=True,
            )
            root_entity = await self._ensure_child_root_entity(
                session,
                user_id=user_id,
                persona_id=None,
                child_name=child_name,
            )
            await self._cleanup_orphan_entities(session, user_id=user_id, root_entity_id=root_entity.id)
            await self.sync_relationships_for_user(session, user_id=user_id)
            duplicate_changes = await self._collapse_duplicate_memories(session, user_id=user_id)
            if duplicate_changes:
                changes.extend(duplicate_changes)
                total_changes += len(duplicate_changes)
            users_processed += 1

        return {
            "users_processed": users_processed,
            "changes_applied": total_changes,
            "changes": changes,
        }

    def is_routine_memory(self, memory: MemoryItem) -> bool:
        return self._memory_world_section(memory) == "daily_routine"

    async def graph_snapshot(
        self,
        session: AsyncSession,
        *,
        user_id: uuid.UUID | str,
        include_archived: bool = False,
        limit: int = 72,
        similarity_limit: int = 2,
    ) -> MemoryGraphResult:
        normalized_user_id = _normalize_uuid(user_id)
        if normalized_user_id is None:
            return MemoryGraphResult(nodes=[], structural_edges=[], similarity_edges=[])

        await self.sync_entity_structure_for_user(session, user_id=normalized_user_id, include_archived=include_archived)
        await self.sync_relationships_for_user(session, user_id=normalized_user_id)
        stmt = select(MemoryItem).where(MemoryItem.user_id == normalized_user_id)
        if not include_archived:
            stmt = stmt.where(MemoryItem.disabled.is_(False))
        stmt = stmt.order_by(desc(MemoryItem.updated_at), desc(MemoryItem.created_at)).limit(max(limit * 3, 180))
        all_memories = list((await session.execute(stmt)).scalars().all())
        memories = [memory for memory in all_memories if self._memory_world_section(memory) == "memories"][: max(limit, 1)]
        if not memories:
            return MemoryGraphResult(nodes=[], structural_edges=[], similarity_edges=[])

        memory_ids = [memory.id for memory in memories]
        memory_links = list(
            (
                await session.execute(
                    select(MemoryItemEntity).where(MemoryItemEntity.memory_id.in_(memory_ids))
                )
            )
            .scalars()
            .all()
        )
        entity_ids = {link.entity_id for link in memory_links}
        root_entity = await session.scalar(
            select(MemoryEntity).where(
                MemoryEntity.user_id == normalized_user_id,
                MemoryEntity.entity_kind == MemoryEntityKind.child,
                MemoryEntity.is_primary.is_(True),
            )
        )
        if root_entity is not None:
            entity_ids.add(root_entity.id)
        relation_rows = []
        if entity_ids:
            relation_rows = list(
                (
                    await session.execute(
                        select(MemoryEntityRelation).where(
                            MemoryEntityRelation.user_id == normalized_user_id,
                            MemoryEntityRelation.parent_entity_id.in_(list(entity_ids)),
                            MemoryEntityRelation.child_entity_id.in_(list(entity_ids)),
                        )
                    )
                )
                .scalars()
                .all()
            )
        entities = {
            entity.id: entity
            for entity in (
                await session.execute(
                    select(MemoryEntity).where(
                        MemoryEntity.user_id == normalized_user_id,
                        MemoryEntity.id.in_(list(entity_ids)),
                    )
                )
            )
            .scalars()
            .all()
        }
        entity_relations = [
            row
            for row in relation_rows
            if row.parent_entity_id in entities and row.child_entity_id in entities
        ]
        structural_rows = list(
            (
                await session.execute(
                    select(MemoryRelationship).where(
                        MemoryRelationship.user_id == normalized_user_id,
                        MemoryRelationship.parent_memory_id.in_(memory_ids),
                        MemoryRelationship.child_memory_id.in_(memory_ids),
                    )
                )
            )
            .scalars()
            .all()
        )
        entity_counts = Counter(link.entity_id for link in memory_links)
        root_entity_id = root_entity.id if root_entity is not None else None
        root_label = root_entity.display_name if root_entity is not None else "Child"
        primary_link_by_memory = {
            link.memory_id: link
            for link in memory_links
            if link.is_primary
        }
        nodes = [
            self._entity_node(
                entity,
                item_count=entity_counts.get(entity.id, 0),
                root_entity_id=root_entity_id,
                root_label=root_label,
            )
            for entity in entities.values()
        ]
        facet_nodes: dict[str, MemoryGraphNode] = {}
        facet_edges: list[MemoryGraphEdge] = []

        def _ensure_facet_node(facet: MemoryFacet | None) -> MemoryGraphNode | None:
            if root_entity_id is None or facet is None:
                return None
            group_key = _facet_group_key(facet)
            if group_key not in facet_nodes:
                facet_nodes[group_key] = self._facet_group_node(
                    group_key,
                    root_entity_id=root_entity_id,
                    root_label=root_label,
                )
                facet_edges.append(self._facet_root_edge(root_entity_id=root_entity_id, facet_key=group_key))
            return facet_nodes[group_key]

        structural_edges: list[MemoryGraphEdge] = []
        for row in entity_relations:
            if root_entity_id is not None and row.parent_entity_id == root_entity_id:
                child_entity = entities.get(row.child_entity_id)
                facet_node = _ensure_facet_node(child_entity.default_facet if child_entity is not None else None)
                if child_entity is not None and facet_node is not None and child_entity.entity_kind != MemoryEntityKind.child:
                    facet_node.item_count += 1
                    facet_edges.append(self._facet_entity_edge(facet_node_id=facet_node.id, relationship=row))
                    continue
            structural_edges.append(self._entity_relation_edge(row))

        for link in memory_links:
            entity = entities.get(link.entity_id)
            if entity is None:
                continue
            if root_entity_id is not None and link.entity_id == root_entity_id and link.is_primary:
                if link.facet != MemoryFacet.identity:
                    facet_node = _ensure_facet_node(link.facet)
                else:
                    facet_node = None
                if facet_node is not None:
                    facet_node.item_count += 1
                    facet_edges.append(self._facet_memory_edge(facet_node_id=facet_node.id, link=link))
                    continue
            structural_edges.append(self._entity_memory_edge(link, entity=entity))

        nodes.extend(facet_nodes.values())
        nodes.extend(
            self._graph_node(
                memory,
                primary_entity=entities.get(primary_link_by_memory[memory.id].entity_id) if memory.id in primary_link_by_memory else None,
                root_entity_id=root_entity_id,
                root_label=root_label,
            )
            for memory in memories
        )
        structural_edges = facet_edges + structural_edges
        structural_edges.extend(self._graph_edge(row) for row in structural_rows)
        structural_pairs = {
            frozenset((row.parent_memory_id, row.child_memory_id))
            for row in structural_rows
        }
        similarity_edges = self._cluster_similarity_edges(
            memories,
            structural_pairs=structural_pairs,
            concept_by_memory=self._concepts_from_entity_links(memory_links, entities),
            per_node_limit=similarity_limit,
        )
        return MemoryGraphResult(
            nodes=nodes,
            structural_edges=structural_edges,
            similarity_edges=similarity_edges,
        )

    async def routine_graph_snapshot(
        self,
        session: AsyncSession,
        *,
        user_id: uuid.UUID | str,
        include_archived: bool = False,
        limit: int = 72,
        similarity_limit: int = 1,
    ) -> MemoryGraphResult:
        normalized_user_id = _normalize_uuid(user_id)
        if normalized_user_id is None:
            return MemoryGraphResult(nodes=[], structural_edges=[], similarity_edges=[])

        await self.sync_entity_structure_for_user(session, user_id=normalized_user_id, include_archived=include_archived)
        await self.sync_relationships_for_user(session, user_id=normalized_user_id)
        stmt = select(MemoryItem).where(MemoryItem.user_id == normalized_user_id)
        if not include_archived:
            stmt = stmt.where(MemoryItem.disabled.is_(False))
        stmt = stmt.order_by(desc(MemoryItem.updated_at), desc(MemoryItem.created_at)).limit(max(limit * 3, 180))
        all_memories = list((await session.execute(stmt)).scalars().all())
        memories = [memory for memory in all_memories if self._memory_world_section(memory) == "daily_routine"][: max(limit, 1)]
        if not memories:
            return MemoryGraphResult(nodes=[], structural_edges=[], similarity_edges=[])

        memory_ids = [memory.id for memory in memories]
        structural_rows = list(
            (
                await session.execute(
                    select(MemoryRelationship).where(
                        MemoryRelationship.user_id == normalized_user_id,
                        MemoryRelationship.parent_memory_id.in_(memory_ids),
                        MemoryRelationship.child_memory_id.in_(memory_ids),
                    )
                )
            )
            .scalars()
            .all()
        )

        timeline_nodes, timeline_edges, week_by_memory = self._time_bucket_graph(memories)
        structural_edges = timeline_edges + [self._graph_edge(row) for row in structural_rows]
        structural_pairs = {
            frozenset((row.parent_memory_id, row.child_memory_id))
            for row in structural_rows
        }
        similarity_edges = self._similarity_edges(
            memories,
            structural_pairs=structural_pairs,
            per_node_limit=similarity_limit,
            week_by_memory=week_by_memory,
        )
        return MemoryGraphResult(
            nodes=timeline_nodes + [self._graph_node(memory) for memory in memories],
            structural_edges=structural_edges,
            similarity_edges=similarity_edges,
        )

    async def memory_inspector(
        self,
        session: AsyncSession,
        *,
        user_id: uuid.UUID | str,
        memory_id: uuid.UUID | str,
    ) -> MemoryInspector | None:
        normalized_user_id = _normalize_uuid(user_id)
        normalized_memory_id = _normalize_uuid(memory_id)
        if normalized_user_id is None or normalized_memory_id is None:
            return None

        await self.sync_entity_structure_for_user(session, user_id=normalized_user_id, include_archived=True)
        await self.sync_relationships_for_user(session, user_id=normalized_user_id)
        memory = await session.scalar(
            select(MemoryItem).where(
                MemoryItem.id == normalized_memory_id,
                MemoryItem.user_id == normalized_user_id,
            )
        )
        if memory is None:
            return None

        relationship_rows = list(
            (
                await session.execute(
                    select(MemoryRelationship).where(
                        MemoryRelationship.user_id == normalized_user_id,
                        or_(
                            MemoryRelationship.parent_memory_id == normalized_memory_id,
                            MemoryRelationship.child_memory_id == normalized_memory_id,
                        ),
                    )
                )
            )
            .scalars()
            .all()
        )
        linked_ids = {
            row.child_memory_id if row.parent_memory_id == normalized_memory_id else row.parent_memory_id
            for row in relationship_rows
        }
        linked_items = {}
        if linked_ids:
            linked_items = {
                item.id: item
                for item in (
                    await session.execute(select(MemoryItem).where(MemoryItem.id.in_(list(linked_ids))))
                )
                .scalars()
                .all()
            }

        attached_links = list(
            (
                await session.execute(
                    select(MemoryItemEntity).where(MemoryItemEntity.memory_id == normalized_memory_id)
                )
            )
            .scalars()
            .all()
        )
        attached_entity_ids = [link.entity_id for link in attached_links]
        attached_entities = {}
        if attached_entity_ids:
            attached_entities = {
                item.id: item
                for item in (
                    await session.execute(select(MemoryEntity).where(MemoryEntity.id.in_(attached_entity_ids)))
                )
                .scalars()
                .all()
            }

        root_entity = await session.scalar(
            select(MemoryEntity).where(
                MemoryEntity.user_id == normalized_user_id,
                MemoryEntity.entity_kind == MemoryEntityKind.child,
                MemoryEntity.is_primary.is_(True),
            )
        )
        linked_memories: list[MemoryLinkedMemory] = []
        for row in relationship_rows:
            linked_id = row.child_memory_id if row.parent_memory_id == normalized_memory_id else row.parent_memory_id
            linked = linked_items.get(linked_id)
            if linked is None:
                continue
            linked_memories.append(
                MemoryLinkedMemory(
                    id=str(linked.id),
                    title=_memory_display_title(linked),
                    summary=_memory_display_summary(linked),
                    kind="structural",
                    relationship_label=_relationship_label(row, focus_memory_id=normalized_memory_id),
                    archived=bool(linked.disabled),
                    pinned=bool(linked.pinned),
                )
            )

        similar_memories = await self._similar_memories_for_inspector(
            session,
            memory=memory,
            user_id=normalized_user_id,
            exclude_ids={normalized_memory_id, *linked_ids},
        )
        linked_memories.extend(similar_memories)

        primary_entity_view = None
        attached_entity_views: list[MemoryEntityView] = []
        for link in attached_links:
            entity = attached_entities.get(link.entity_id)
            if entity is None:
                continue
            link_semantic = self._semantic_payload_from_dict(link.semantic_json)
            entity_view = MemoryEntityView(
                id=str(entity.id),
                display_name=entity.display_name,
                entity_kind=entity.entity_kind.value,
                facet=link.facet.value,
                relation_to_child=entity.relation_to_child,
                role=link.role,
                world_section=(link_semantic.world_section if link_semantic else None),
                semantic_group=(link_semantic.group if link_semantic else None),
                semantic_label=(link_semantic.label if link_semantic else None),
                semantic_relation=(link_semantic.relation if link_semantic else None),
                semantic_path=(list(link_semantic.path or []) if link_semantic else []),
            )
            attached_entity_views.append(entity_view)
            if link.is_primary and primary_entity_view is None:
                primary_entity_view = entity_view

        memory_semantic = self._semantic_payload_from_memory(memory)
        primary_entity_model = primary_entity_view
        primary_entity = None
        if primary_entity_model is not None:
            primary_entity = attached_entities.get(_normalize_uuid(primary_entity_model.id))
        return MemoryInspector(
            id=str(memory.id),
            title=_memory_display_title(memory),
            memory_type=memory.memory_type.value,
            memory_type_label=_memory_type_label(memory.memory_type),
            content=memory.content,
            summary=memory.summary,
            tags=list(memory.tags or []),
            pinned=bool(memory.pinned),
            archived=bool(memory.disabled),
            importance_score=float(memory.importance_score or 0.0),
            updated_at=memory.updated_at.isoformat() if memory.updated_at else None,
            world_section=self._memory_world_section(memory),
            semantic_group=memory_semantic.group if memory_semantic else None,
            semantic_label=memory_semantic.label if memory_semantic else None,
            semantic_relation=memory_semantic.relation if memory_semantic else None,
            semantic_path=list(memory_semantic.path or []) if memory_semantic else [],
            primary_entity=primary_entity_model,
            attached_entities=attached_entity_views,
            linked_memories=linked_memories,
            breadcrumb=self._graph_breadcrumb_for_memory(
                memory,
                primary_entity=primary_entity,
                root_entity_id=root_entity.id if root_entity is not None else None,
                root_label=root_entity.display_name if root_entity is not None else None,
            ),
            icon_key=self._icon_key_for_node(
                kind="memory",
                facet=(primary_entity.default_facet.value if primary_entity is not None else None),
                memory_type=memory.memory_type.value,
            ),
        )

    async def update_memory_for_parent(
        self,
        session: AsyncSession,
        *,
        user_id: uuid.UUID | str,
        memory_id: uuid.UUID | str,
        data: dict[str, Any],
        config: dict[str, Any],
    ) -> MemoryInspector | None:
        normalized_user_id = _normalize_uuid(user_id)
        normalized_memory_id = _normalize_uuid(memory_id)
        if normalized_user_id is None or normalized_memory_id is None:
            return None

        await self.sync_entity_structure_for_user(session, user_id=normalized_user_id, include_archived=True)
        memory = await session.scalar(
            select(MemoryItem).where(
                MemoryItem.id == normalized_memory_id,
                MemoryItem.user_id == normalized_user_id,
            )
        )
        if memory is None:
            return None

        text_changed = False
        title = _normalize_optional_text(data.get("title"))
        content = _normalize_required_text(data.get("content"))
        summary = _normalize_optional_text(data.get("summary"))
        tags = _normalize_tags(data.get("tags"))
        pinned = _coerce_bool(data.get("pinned"), default=False)
        archived = _coerce_bool(data.get("archived"), default=False)
        subject_name = _normalize_optional_text(data.get("subject_name"))
        entity_kind = _normalize_optional_text(data.get("entity_kind"))
        facet = _normalize_optional_text(data.get("facet"))
        relation_to_child = _normalize_optional_text(data.get("relation_to_child"))

        if content is None:
            raise ValueError("Memory text can't be blank.")

        if memory.title != title:
            memory.title = title
            text_changed = True
        if memory.content != content:
            memory.content = content
            text_changed = True
        if memory.summary != summary:
            memory.summary = summary
            text_changed = True
        if list(memory.tags or []) != tags:
            memory.tags = tags
            text_changed = True
        memory.pinned = pinned
        memory.disabled = archived
        metadata_json = dict(memory.metadata_json or {})
        structured_override = {
            "subject_name": subject_name,
            "entity_kind": entity_kind,
            "facet": facet,
            "relation_to_child": relation_to_child,
        }
        if any(value for value in structured_override.values()):
            metadata_json["structured_override"] = structured_override
        elif "structured_override" in metadata_json:
            metadata_json.pop("structured_override", None)
        metadata_json["parent_last_edited_at"] = utc_now().isoformat()
        memory.metadata_json = metadata_json

        await session.flush()
        if text_changed:
            await self.embed_items(session, [memory], config=config)
        await self.ensure_structure_for_memories(
            session,
            user_id=normalized_user_id,
            persona_id=memory.persona_id,
            memories=[memory],
            child_name=await self._resolve_child_name(session, user_id=normalized_user_id),
        )
        await self.sync_relationships_for_user(session, user_id=normalized_user_id)
        return await self.memory_inspector(session, user_id=normalized_user_id, memory_id=normalized_memory_id)

    async def delete_preview_for_parent(
        self,
        session: AsyncSession,
        *,
        user_id: uuid.UUID | str,
        memory_id: uuid.UUID | str,
    ) -> MemoryDeletePreview | None:
        normalized_user_id = _normalize_uuid(user_id)
        normalized_memory_id = _normalize_uuid(memory_id)
        if normalized_user_id is None or normalized_memory_id is None:
            return None

        await self.sync_relationships_for_user(session, user_id=normalized_user_id)
        memories = list(
            (
                await session.execute(select(MemoryItem).where(MemoryItem.user_id == normalized_user_id))
            )
            .scalars()
            .all()
        )
        by_id = {memory.id: memory for memory in memories}
        if normalized_memory_id not in by_id:
            return None

        relationship_rows = list(
            (
                await session.execute(
                    select(MemoryRelationship).where(
                        MemoryRelationship.user_id == normalized_user_id,
                        MemoryRelationship.relationship_type.in_(_CASCADE_RELATIONSHIP_TYPES),
                    )
                )
            )
            .scalars()
            .all()
        )
        children_by_parent: dict[uuid.UUID, list[tuple[uuid.UUID, MemoryRelationshipType]]] = {}
        parents_by_child: dict[uuid.UUID, set[uuid.UUID]] = {}
        for row in relationship_rows:
            children_by_parent.setdefault(row.parent_memory_id, []).append((row.child_memory_id, row.relationship_type))
            parents_by_child.setdefault(row.child_memory_id, set()).add(row.parent_memory_id)

        ordered_ids: list[uuid.UUID] = [normalized_memory_id]
        reasons: dict[uuid.UUID, str] = {
            normalized_memory_id: "Selected memory",
        }
        queued: list[uuid.UUID] = [normalized_memory_id]
        to_delete: set[uuid.UUID] = {normalized_memory_id}

        while queued:
            current_id = queued.pop(0)
            for child_id, relationship_type in children_by_parent.get(current_id, []):
                if child_id in to_delete:
                    continue
                surviving_parents = {
                    parent_id
                    for parent_id in parents_by_child.get(child_id, set())
                    if parent_id not in to_delete
                }
                if surviving_parents:
                    continue
                to_delete.add(child_id)
                ordered_ids.append(child_id)
                reasons[child_id] = _cascade_reason(
                    relationship_type,
                    parent_title=_memory_display_title(by_id.get(current_id)),
                )
                queued.append(child_id)

        affected = [
            MemoryDeletePreviewEntry(
                id=str(item_id),
                title=_memory_display_title(by_id[item_id]),
                reason=reasons[item_id],
            )
            for item_id in ordered_ids
            if item_id in by_id
        ]
        return MemoryDeletePreview(
            memory_id=str(normalized_memory_id),
            deleted_count=len(affected),
            affected=affected,
        )

    async def delete_memory_for_parent(
        self,
        session: AsyncSession,
        *,
        user_id: uuid.UUID | str,
        memory_id: uuid.UUID | str,
    ) -> MemoryDeletePreview | None:
        normalized_user_id = _normalize_uuid(user_id)
        if normalized_user_id is None:
            return None
        preview = await self.delete_preview_for_parent(session, user_id=normalized_user_id, memory_id=memory_id)
        if preview is None:
            return None

        deletion_ids = {_normalize_uuid(entry.id) for entry in preview.affected}
        deletion_ids.discard(None)
        if not deletion_ids:
            return preview

        survivors = list(
            (
                await session.execute(
                    select(MemoryItem).where(
                        MemoryItem.user_id == normalized_user_id,
                        MemoryItem.id.not_in(list(deletion_ids)),
                    )
                )
            )
            .scalars()
            .all()
        )
        for item in survivors:
            metadata_json = dict(item.metadata_json or {})
            changed = False
            if item.consolidated_into_id in deletion_ids:
                item.consolidated_into_id = None
                changed = True
            if _normalize_uuid(metadata_json.get("supersedes_id")) in deletion_ids:
                metadata_json.pop("supersedes_id", None)
                changed = True
            if _normalize_uuid(metadata_json.get("superseded_by_id")) in deletion_ids:
                metadata_json.pop("superseded_by_id", None)
                changed = True
            if changed:
                item.metadata_json = metadata_json

        await session.execute(
            delete(MemoryItemEntity).where(
                MemoryItemEntity.memory_id.in_(list(deletion_ids))
            )
        )
        await session.execute(
            delete(MemoryRelationship).where(
                MemoryRelationship.user_id == normalized_user_id,
                or_(
                    MemoryRelationship.parent_memory_id.in_(list(deletion_ids)),
                    MemoryRelationship.child_memory_id.in_(list(deletion_ids)),
                ),
            )
        )
        await session.execute(
            delete(MemoryItem).where(
                MemoryItem.user_id == normalized_user_id,
                MemoryItem.id.in_(list(deletion_ids)),
            )
        )
        await session.flush()
        await self.sync_relationships_for_user(session, user_id=normalized_user_id)
        child_name = await self._resolve_child_name(session, user_id=normalized_user_id)
        await self.sync_entity_structure_for_user(session, user_id=normalized_user_id, child_name=child_name, include_archived=True)
        return preview

    async def clear_memory_store(
        self,
        session: AsyncSession,
        *,
        user_id: uuid.UUID | str,
    ) -> dict[str, int]:
        normalized_user_id = _normalize_uuid(user_id)
        if normalized_user_id is None:
            return {"deleted_memories": 0, "deleted_entities": 0}

        memory_count = int(
            await session.scalar(
                select(func.count()).select_from(MemoryItem).where(MemoryItem.user_id == normalized_user_id)
            )
            or 0
        )
        entity_count = int(
            await session.scalar(
                select(func.count()).select_from(MemoryEntity).where(MemoryEntity.user_id == normalized_user_id)
            )
            or 0
        )
        memory_ids = select(MemoryItem.id).where(MemoryItem.user_id == normalized_user_id)
        entity_ids = select(MemoryEntity.id).where(MemoryEntity.user_id == normalized_user_id)

        await session.execute(delete(MemoryItemEntity).where(MemoryItemEntity.memory_id.in_(memory_ids)))
        await session.execute(delete(MemoryRelationship).where(MemoryRelationship.user_id == normalized_user_id))
        await session.execute(delete(MemoryEntityRelation).where(MemoryEntityRelation.user_id == normalized_user_id))
        await session.execute(delete(MemoryEntity).where(MemoryEntity.id.in_(entity_ids)))
        await session.execute(delete(MemoryItem).where(MemoryItem.id.in_(memory_ids)))
        await session.flush()
        return {
            "deleted_memories": memory_count,
            "deleted_entities": entity_count,
        }

    def _embedding_text(self, item: MemoryItem) -> str:
        tags = ", ".join(item.tags or [])
        metadata = item.metadata_json or {}
        semantic = self._semantic_payload_from_memory(item)
        entity_name = metadata.get("entity_name") or ""
        entity_kind = metadata.get("entity_kind") or ""
        facet = metadata.get("facet") or ""
        relation_to_child = metadata.get("relation_to_child") or ""
        return (
            f"type={item.memory_type.value}\n"
            f"title={item.title or ''}\n"
            f"entity_name={entity_name}\n"
            f"entity_kind={entity_kind}\n"
            f"facet={facet}\n"
            f"relation_to_child={relation_to_child}\n"
            f"world_section={semantic.world_section if semantic else ''}\n"
            f"semantic_group={semantic.group if semantic else ''}\n"
            f"semantic_label={semantic.label if semantic else ''}\n"
            f"semantic_path={', '.join(semantic.path) if semantic else ''}\n"
            f"tags={tags}\n"
            f"content={item.content}"
        )

    def _heuristic_facts(self, body: str) -> list[dict[str, Any]]:
        lowered = body.lower()
        triggers = ["i like", "i love", "my favorite", "remember", "i prefer", "i am "]
        if any(token in lowered for token in triggers):
            return [
                {
                    "memory_type": "fact",
                    "title": "User shared something important",
                    "content": body.strip()[:400],
                    "summary": body.strip()[:160],
                    "tags": ["heuristic"],
                    "importance_score": 0.55,
                }
            ]
        return []

    async def _build_or_merge_memory_item(
        self,
        session: AsyncSession,
        *,
        user: User,
        persona: Persona | None,
        source_message: Message,
        raw_item: dict[str, Any],
        config: dict[str, Any],
    ) -> MemoryItem | None:
        content = str(raw_item.get("content", "")).strip()
        if not content:
            return None
        metadata_json = {
            "source": "extraction",
            "confidence": float(raw_item.get("confidence", 0.65)),
            "temporal_scope": str(raw_item.get("temporal_scope", "durable")),
            "supersedes_id": str(raw_item.get("supersedes_id", "")).strip() or None,
            "facet": str(raw_item.get("facet", "")).strip() or None,
            "relation_to_child": str(raw_item.get("relation_to_child", "")).strip() or None,
            "canonical_value": str(raw_item.get("canonical_value", "")).strip() or None,
        }
        entity_name = str(raw_item.get("entity_name", "")).strip()
        entity_kind = str(raw_item.get("entity_kind", "")).strip()
        should_profile = bool(raw_item.get("should_profile")) and bool(entity_name)
        if should_profile:
            metadata_json.update(
                {
                    "entity_name": entity_name,
                    "entity_name_normalized": entity_name.casefold(),
                    "entity_kind": entity_kind or "topic",
                    "memory_scope": "entity",
                }
            )
            merged = await self._merge_entity_memory(
                session,
                user=user,
                persona=persona,
                source_message=source_message,
                raw_item=raw_item,
                metadata_json=metadata_json,
            )
            if merged is not None:
                return merged

        memory = MemoryItem(
            user_id=user.id,
            persona_id=persona.id if persona else None,
            source_message_id=source_message.id,
            memory_type=MemoryType(raw_item.get("memory_type", "fact")),
            title=raw_item.get("title"),
            content=content,
            summary=raw_item.get("summary"),
            tags=raw_item.get("tags", []),
            importance_score=float(raw_item.get("importance_score", 0.5)),
            metadata_json=metadata_json,
        )
        session.add(memory)
        await session.flush()
        await self._apply_supersession_if_needed(
            session,
            user=user,
            memory=memory,
            raw_item=raw_item,
            metadata_json=metadata_json,
        )
        return memory

    async def _merge_entity_memory(
        self,
        session: AsyncSession,
        *,
        user: User,
        persona: Persona | None,
        source_message: Message,
        raw_item: dict[str, Any],
        metadata_json: dict[str, Any],
    ) -> MemoryItem | None:
        entity_name = str(metadata_json.get("entity_name") or "").strip()
        if not entity_name:
            return None
        normalized_name = entity_name.casefold()
        stmt = (
            select(MemoryItem)
            .where(
                MemoryItem.user_id == user.id,
                MemoryItem.disabled.is_(False),
            )
            .order_by(desc(MemoryItem.updated_at))
            .limit(100)
        )
        candidates = list((await session.execute(stmt)).scalars().all())
        if persona is not None:
            candidates = [item for item in candidates if item.persona_id in (None, persona.id)]
        entity_candidates = [
            item
            for item in candidates
            if (item.metadata_json or {}).get("entity_name_normalized") == normalized_name
        ]
        if not entity_candidates:
            return None
        existing = entity_candidates[0]
        merged_data = await self._entity_merge_decision(existing=existing, raw_item=raw_item, source_message=source_message)
        if not merged_data.get("same_entity", True):
            return None
        existing.title = str(merged_data.get("title") or existing.title or raw_item.get("title") or entity_name)[:120]
        existing.content = str(merged_data.get("content") or existing.content).strip()
        existing.summary = str(merged_data.get("summary") or existing.summary or existing.content[:160]).strip()
        existing.tags = _merge_tags(existing.tags, raw_item.get("tags", []), merged_data.get("tags", []))
        existing.importance_score = max(
            float(existing.importance_score or 0.0),
            float(raw_item.get("importance_score", 0.5)),
            float(merged_data.get("importance_score", 0.0) or 0.0),
        )
        existing.source_message_id = source_message.id
        existing.metadata_json = {
            **(existing.metadata_json or {}),
            **metadata_json,
            "source": "entity_merge",
            "merge_count": int((existing.metadata_json or {}).get("merge_count", 0)) + 1,
            "last_fact_added": str(raw_item.get("content", "")).strip()[:300],
        }
        logger.info(
            "memory_merged",
            memory_id=str(existing.id),
            user_id=str(existing.user_id) if existing.user_id else None,
            persona_id=str(existing.persona_id) if existing.persona_id else None,
            memory_type=existing.memory_type.value,
            title=existing.title,
            summary=existing.summary,
            tags=existing.tags,
            entity_name=(existing.metadata_json or {}).get("entity_name"),
            entity_kind=(existing.metadata_json or {}).get("entity_kind"),
            merge_count=(existing.metadata_json or {}).get("merge_count"),
            last_fact_added=(existing.metadata_json or {}).get("last_fact_added"),
        )
        return existing

    async def _entity_merge_decision(
        self,
        *,
        existing: MemoryItem,
        raw_item: dict[str, Any],
        source_message: Message,
    ) -> dict[str, Any]:
        if not self.ai_runtime.enabled:
            return {
                "same_entity": True,
                "title": existing.title or raw_item.get("title"),
                "content": _merge_text(existing.content, str(raw_item.get("content", ""))),
                "summary": raw_item.get("summary") or existing.summary,
                "tags": raw_item.get("tags", []),
                "importance_score": raw_item.get("importance_score", 0.5),
            }
        try:
            response = await self.ai_runtime.merge_entity_memory(
                prompt=(
                    "Decide whether this new memory candidate is about the same entity as the existing entity memory, "
                    "and if so merge it into an updated compact profile.\n"
                    "- Prefer same_entity=true when the names clearly match and the new fact adds detail.\n"
                    "- Keep merged content compact but cumulative, like a living memory/profile.\n"
                    "- Include only factual information that has actually been mentioned.\n\n"
                    f"Existing memory title: {existing.title or ''}\n"
                    f"Existing memory content: {existing.content}\n"
                    f"Existing summary: {existing.summary or ''}\n\n"
                    f"New message: {source_message.body or ''}\n"
                    f"New candidate title: {raw_item.get('title', '')}\n"
                    f"New candidate content: {raw_item.get('content', '')}\n"
                    f"New candidate summary: {raw_item.get('summary', '')}\n"
                    f"New candidate tags: {raw_item.get('tags', [])}\n"
                ),
                max_tokens=self.settings.openai.memory_max_output_tokens,
            )
            return response.output.model_dump(mode="json")
        except Exception:
            pass
        return {
            "same_entity": True,
            "title": existing.title or raw_item.get("title"),
            "content": _merge_text(existing.content, str(raw_item.get("content", ""))),
            "summary": raw_item.get("summary") or existing.summary,
            "tags": raw_item.get("tags", []),
            "importance_score": raw_item.get("importance_score", 0.5),
        }

    async def _apply_supersession_if_needed(
        self,
        session: AsyncSession,
        *,
        user: User,
        memory: MemoryItem,
        raw_item: dict[str, Any],
        metadata_json: dict[str, Any],
    ) -> None:
        explicit = metadata_json.get("supersedes_id")
        target_id = explicit or raw_item.get("supersedes_id")
        if target_id:
            existing = await session.get(MemoryItem, target_id)
            if existing and existing.user_id == user.id:
                existing.disabled = True
                current_meta = dict(existing.metadata_json or {})
                current_meta["superseded_by_id"] = str(memory.id)
                existing.metadata_json = current_meta
            return
        if memory.memory_type != MemoryType.preference:
            return
        stmt = (
            select(MemoryItem)
            .where(
                MemoryItem.user_id == user.id,
                MemoryItem.disabled.is_(False),
                MemoryItem.memory_type == MemoryType.preference,
            )
            .order_by(desc(MemoryItem.created_at))
            .limit(20)
        )
        candidates = list((await session.execute(stmt)).scalars().all())
        lowered_new = memory.content.lower()
        contradiction_tokens = ("not anymore", "no longer", "used to", "instead now", "stopped")
        if not any(token in lowered_new for token in contradiction_tokens):
            return
        for existing in candidates:
            if existing.id == memory.id:
                continue
            if any(token in existing.content.lower() for token in ("like", "love", "prefer", "favorite")):
                existing.disabled = True
                current_meta = dict(existing.metadata_json or {})
                current_meta["superseded_by_id"] = str(memory.id)
                existing.metadata_json = current_meta
                break

    def _apply_retrieval_penalties(self, results: list[RetrievedMemory]) -> list[RetrievedMemory]:
        adjusted: list[RetrievedMemory] = []
        now = utc_now()
        for item in results:
            score = float(item.score)
            if item.memory.last_accessed_at:
                minutes = (now - item.memory.last_accessed_at).total_seconds() / 60.0
                if minutes < 45:
                    score -= 0.18
                elif minutes < 120:
                    score -= 0.08
            score -= min(float(item.memory.retrieval_count or 0) * 0.005, 0.12)
            adjusted.append(
                RetrievedMemory(
                    memory=item.memory,
                    score=score,
                    explanation=f"{item.explanation}|anti_loop",
                )
            )
        adjusted.sort(
            key=lambda item: (item.memory.pinned, item.score, item.memory.importance_score),
            reverse=True,
        )
        return adjusted

    def _icon_key_for_node(
        self,
        *,
        kind: str,
        facet: str | None = None,
        memory_type: str | None = None,
    ) -> str:
        if kind in {"child", "family_member", "friend", "pet", "artist", "activity", "health_context", "topic", "memory", "week", "day"}:
            return "events" if kind in {"week", "day"} else kind
        if kind == "facet":
            if facet == "family":
                return "family"
            if facet == "friends":
                return "friend"
            if facet == "pets":
                return "pet"
            if facet in {"likes_and_preferences", "preferences", "favorites"}:
                return "favorites"
            if facet in {"events", "milestones"}:
                return "events"
            if facet == "health_context":
                return "health"
        if kind == "memory" and memory_type in {"preference", "fact", "summary", "episode"}:
            return "memory"
        return "section"

    def _graph_breadcrumb_for_facet(
        self,
        facet_key: str,
        *,
        root_entity_id: uuid.UUID,
        root_label: str | None,
    ) -> list[MemoryInspectorBreadcrumb]:
        root_name = root_label or "Child"
        return [
            MemoryInspectorBreadcrumb(id=str(root_entity_id), label=root_name, kind="node"),
            MemoryInspectorBreadcrumb(id=f"facet:{root_entity_id}:{facet_key}", label=_facet_group_label(facet_key), kind="branch"),
        ]

    def _graph_breadcrumb_for_entity(
        self,
        entity: MemoryEntity,
        *,
        root_entity_id: uuid.UUID | None,
        root_label: str | None,
    ) -> list[MemoryInspectorBreadcrumb]:
        if entity.entity_kind == MemoryEntityKind.child:
            return [MemoryInspectorBreadcrumb(id=str(entity.id), label=entity.display_name, kind="node")]
        breadcrumbs: list[MemoryInspectorBreadcrumb] = []
        if root_entity_id is not None:
            breadcrumbs.append(
                MemoryInspectorBreadcrumb(
                    id=str(root_entity_id),
                    label=root_label or "Child",
                    kind="node",
                )
            )
            facet_key = _facet_group_key(entity.default_facet)
            if facet_key != "identity":
                breadcrumbs.append(
                    MemoryInspectorBreadcrumb(
                        id=f"facet:{root_entity_id}:{facet_key}",
                        label=_facet_group_label(facet_key),
                        kind="branch",
                    )
                )
        breadcrumbs.append(MemoryInspectorBreadcrumb(id=str(entity.id), label=entity.display_name, kind="node"))
        return breadcrumbs

    def _graph_breadcrumb_for_memory(
        self,
        memory: MemoryItem,
        *,
        primary_entity: MemoryEntity | None,
        root_entity_id: uuid.UUID | None,
        root_label: str | None,
    ) -> list[MemoryInspectorBreadcrumb]:
        breadcrumbs: list[MemoryInspectorBreadcrumb] = []
        if primary_entity is not None:
            breadcrumbs.extend(
                self._graph_breadcrumb_for_entity(
                    primary_entity,
                    root_entity_id=root_entity_id,
                    root_label=root_label,
                )
            )
        elif root_entity_id is not None:
            breadcrumbs.append(
                MemoryInspectorBreadcrumb(
                    id=str(root_entity_id),
                    label=root_label or "Child",
                    kind="node",
                )
            )
        breadcrumbs.append(
            MemoryInspectorBreadcrumb(
                id=str(memory.id),
                label=_memory_display_title(memory),
                kind="memory",
            )
        )
        return breadcrumbs

    def _entity_node(
        self,
        entity: MemoryEntity,
        *,
        item_count: int,
        root_entity_id: uuid.UUID | None = None,
        root_label: str | None = None,
    ) -> MemoryGraphNode:
        kind = "child" if entity.entity_kind == MemoryEntityKind.child else entity.entity_kind.value
        semantic = self._semantic_payload_from_dict(entity.semantic_json)
        summary = self._entity_graph_summary(entity, item_count=item_count, semantic=semantic)
        return MemoryGraphNode(
            id=str(entity.id),
            label=entity.display_name,
            kind=kind,
            memory_type=entity.entity_kind.value,
            memory_type_label=entity.entity_kind.value.replace("_", " ").title(),
            summary=summary,
            entity_id=str(entity.id),
            entity_kind=entity.entity_kind.value,
            facet=entity.default_facet.value,
            relation_to_child=entity.relation_to_child,
            item_count=item_count,
            updated_at=entity.updated_at.isoformat() if entity.updated_at else None,
            world_section=(semantic.world_section if semantic else None),
            semantic_group=(semantic.group if semantic else None),
            semantic_label=(semantic.label if semantic else None),
            semantic_relation=(semantic.relation if semantic else None),
            semantic_path=(list(semantic.path or []) if semantic else []),
            breadcrumb=self._graph_breadcrumb_for_entity(entity, root_entity_id=root_entity_id, root_label=root_label),
            icon_key=self._icon_key_for_node(kind=kind, facet=entity.default_facet.value),
            branch_label=_facet_group_label(_facet_group_key(entity.default_facet)),
        )

    def _entity_graph_summary(
        self,
        entity: MemoryEntity,
        *,
        item_count: int,
        semantic: MemorySemanticPayload | None,
    ) -> str:
        relation = str(entity.relation_to_child or "").replace("_", " ").strip()
        kind_label = entity.entity_kind.value.replace("_", " ")
        connected_label = f"{item_count} connected memor{'y' if item_count == 1 else 'ies'}"

        if entity.entity_kind == MemoryEntityKind.child:
            return (
                f"{entity.display_name} is the main anchor for this memory world. "
                f"Core details, relationships, and important themes branch out from here, with {connected_label} attached directly."
            )

        if relation:
            prefix = f"{entity.display_name} is tracked here as {relation} in the child's world."
        else:
            prefix = f"{entity.display_name} is a {kind_label} node in the child's world."

        detail_bits: list[str] = []
        if entity.canonical_value and entity.canonical_value != entity.display_name:
            detail_bits.append(f"Stored value: {entity.canonical_value}.")
        if semantic and semantic.group and semantic.group not in {semantic.kind, kind_label, entity.default_facet.value}:
            detail_bits.append(f"It sits within {semantic.group.replace('_', ' ')}.")
        detail_bits.append(f"It currently connects to {connected_label}.")
        return " ".join([prefix, *detail_bits]).strip()

    def _entity_relation_edge(self, relationship: MemoryEntityRelation) -> MemoryGraphEdge:
        return MemoryGraphEdge(
            id=f"entity:{relationship.id}",
            source=str(relationship.parent_entity_id),
            target=str(relationship.child_entity_id),
            kind="structural",
            relationship_type=relationship.relationship_kind.value,
            label=relationship.relationship_kind.value.replace("_", " "),
        )

    def _entity_memory_edge(self, link: MemoryItemEntity, *, entity: MemoryEntity) -> MemoryGraphEdge:
        return MemoryGraphEdge(
            id=f"entity-memory:{entity.id}:{link.memory_id}:{link.role}",
            source=str(entity.id),
            target=str(link.memory_id),
            kind="structural",
            relationship_type="entity_memory_primary" if link.is_primary else "entity_memory_related",
            label=link.facet.value.replace("_", " "),
        )

    def _facet_group_node(
        self,
        facet_key: str,
        *,
        root_entity_id: uuid.UUID,
        root_label: str | None = None,
    ) -> MemoryGraphNode:
        label = _facet_group_label(facet_key)
        return MemoryGraphNode(
            id=f"facet:{root_entity_id}:{facet_key}",
            label=label,
            kind="facet",
            memory_type=facet_key,
            memory_type_label=label,
            summary=_facet_group_summary(facet_key),
            facet=facet_key,
            breadcrumb=self._graph_breadcrumb_for_facet(
                facet_key,
                root_entity_id=root_entity_id,
                root_label=root_label,
            ),
            icon_key=self._icon_key_for_node(kind="facet", facet=facet_key),
            branch_label=label,
        )

    def _facet_root_edge(self, *, root_entity_id: uuid.UUID, facet_key: str) -> MemoryGraphEdge:
        return MemoryGraphEdge(
            id=f"facet-root:{root_entity_id}:{facet_key}",
            source=str(root_entity_id),
            target=f"facet:{root_entity_id}:{facet_key}",
            kind="structural",
            relationship_type="facet_group",
            label=_facet_group_label(facet_key),
        )

    def _facet_entity_edge(self, *, facet_node_id: str, relationship: MemoryEntityRelation) -> MemoryGraphEdge:
        return MemoryGraphEdge(
            id=f"facet-entity:{facet_node_id}:{relationship.id}",
            source=facet_node_id,
            target=str(relationship.child_entity_id),
            kind="structural",
            relationship_type="facet_entity",
            label=relationship.relationship_kind.value.replace("_", " "),
        )

    def _facet_memory_edge(self, *, facet_node_id: str, link: MemoryItemEntity) -> MemoryGraphEdge:
        return MemoryGraphEdge(
            id=f"facet-memory:{facet_node_id}:{link.memory_id}:{link.role}",
            source=facet_node_id,
            target=str(link.memory_id),
            kind="structural",
            relationship_type="facet_memory",
            label=link.facet.value.replace("_", " "),
        )

    def _concepts_from_entity_links(
        self,
        memory_links: list[MemoryItemEntity],
        entities: dict[uuid.UUID, MemoryEntity],
    ) -> dict[uuid.UUID, str | None]:
        concepts: dict[uuid.UUID, str | None] = {}
        for link in memory_links:
            if not link.is_primary:
                continue
            entity = entities.get(link.entity_id)
            if entity is None:
                continue
            concepts[link.memory_id] = f"entity:{entity.id}"
        return concepts

    def _memory_world_section(self, memory: MemoryItem) -> str:
        semantic = self._semantic_payload_from_memory(memory)
        if semantic is not None and semantic.world_section:
            return semantic.world_section
        metadata = dict(memory.metadata_json or {})
        structured_override = metadata.get("structured_override")
        if isinstance(structured_override, dict):
            override_semantic = structured_override.get("semantic")
            if isinstance(override_semantic, dict):
                world_section = str(override_semantic.get("world_section") or "").strip()
                if world_section:
                    return world_section
        if _is_daily_routine_memory(memory):
            return "daily_routine"
        return "memories"

    def _memory_is_routine(self, memory: MemoryItem) -> bool:
        return self._memory_world_section(memory) == "daily_routine"

    async def _determine_structured_placement(
        self,
        session: AsyncSession,
        *,
        memory: MemoryItem,
        child_name: str,
        root_entity: MemoryEntity,
    ) -> StructuredPlacement:
        metadata = dict(memory.metadata_json or {})
        structured_override = metadata.get("structured_override") if isinstance(metadata.get("structured_override"), dict) else {}
        content = (memory.content or "").strip()
        title = (memory.title or "").strip()
        summary = (memory.summary or "").strip()
        lowered = normalize_text_fragment(" ".join([title, summary, content]))
        entity_name = str(structured_override.get("subject_name") or metadata.get("entity_name") or "").strip()
        entity_kind_raw = str(structured_override.get("entity_kind") or metadata.get("entity_kind") or "").strip().lower()
        facet_raw = str(structured_override.get("facet") or metadata.get("facet") or "").strip().lower()
        relation_override = str(structured_override.get("relation_to_child") or metadata.get("relation_to_child") or "").strip()
        provenance_source = str(metadata.get("source_kind") or metadata.get("source") or "memory")
        facet_override = _coerce_memory_facet(facet_raw)
        explicit_related_entities = _related_entities_from_payload(structured_override.get("related_entities"))
        existing_entities = await self._placement_candidate_entities(
            session,
            user_id=root_entity.user_id,
            text=" ".join([title, summary, content]),
        )

        if entity_name or entity_kind_raw or facet_override or relation_override or explicit_related_entities:
            entity_kind = _coerce_entity_kind(entity_kind_raw) if entity_kind_raw else (
                MemoryEntityKind.topic if entity_name else MemoryEntityKind.child
            )
            placement = StructuredPlacement(
                primary_name=entity_name if entity_kind != MemoryEntityKind.child else None,
                primary_kind=entity_kind,
                facet=facet_override or _infer_memory_facet(memory, lowered, default=_default_facet_for_entity_kind(entity_kind)),
                relation_to_child=relation_override or None,
                relation_kind=_coerce_relation_kind(relation_override, entity_kind=entity_kind),
                provenance_source=provenance_source,
                canonical_value=summary or content[:180],
                related_entities=explicit_related_entities,
            )
            return _refine_placement_with_existing_entities(
                placement,
                memory=memory,
                lowered=lowered,
                existing_entities=existing_entities,
                child_name=child_name,
            )

        inferred = await self._infer_structured_placement_with_ai(
            session,
            memory=memory,
            child_name=child_name,
            provenance_source=provenance_source,
            existing_entities=existing_entities,
        )
        if inferred is not None:
            return _refine_placement_with_existing_entities(
                inferred,
                memory=memory,
                lowered=lowered,
                existing_entities=existing_entities,
                child_name=child_name,
            )

        topic_name = _topic_entity_name(memory)
        if topic_name:
            placement = StructuredPlacement(
                primary_name=topic_name,
                primary_kind=MemoryEntityKind.topic,
                facet=facet_override or _infer_memory_facet(memory, lowered, default=MemoryFacet.preferences if memory.memory_type == MemoryType.preference else MemoryFacet.events),
                relation_kind=EntityRelationKind.related,
                provenance_source=provenance_source,
                canonical_value=summary or content[:180],
            )
            return _refine_placement_with_existing_entities(
                placement,
                memory=memory,
                lowered=lowered,
                existing_entities=existing_entities,
                child_name=child_name,
            )

        placement = StructuredPlacement(
            primary_name=None,
            primary_kind=MemoryEntityKind.child,
            facet=facet_override or _infer_memory_facet(memory, lowered, default=MemoryFacet.preferences if memory.memory_type == MemoryType.preference else MemoryFacet.events),
            relation_kind=EntityRelationKind.child_world,
            provenance_source=provenance_source,
            canonical_value=summary or content[:180],
        )
        return _refine_placement_with_existing_entities(
            placement,
            memory=memory,
            lowered=lowered,
            existing_entities=existing_entities,
            child_name=child_name,
        )

    async def _infer_structured_placement_with_ai(
        self,
        session: AsyncSession,
        *,
        memory: MemoryItem,
        child_name: str,
        provenance_source: str,
        existing_entities: list[MemoryEntity],
    ) -> StructuredPlacement | None:
        if not getattr(self.ai_runtime, "enabled", False) or not hasattr(self.ai_runtime, "infer_memory_placement"):
            return None
        try:
            prompt = await self.prompt_service.render(
                session,
                "memory_structured_placement",
                {
                    "child_name": child_name,
                    "memory": memory,
                    "memory_type": memory.memory_type.value,
                    "metadata": dict(memory.metadata_json or {}),
                    "existing_entities": [
                        {
                            "display_name": entity.display_name,
                            "entity_kind": entity.entity_kind.value,
                            "facet": entity.default_facet.value,
                            "relation_to_child": entity.relation_to_child,
                            "canonical_value": entity.canonical_value,
                        }
                        for entity in existing_entities[:10]
                    ],
                },
            )
            response = await self.ai_runtime.infer_memory_placement(prompt=prompt, max_tokens=260)
        except Exception:
            logger.debug("structured placement inference failed", exc_info=True)
            return None

        lowered = normalize_text_fragment(" ".join([memory.title or "", memory.summary or "", memory.content or ""]))
        return _placement_from_ai_draft(
            response.output,
            memory=memory,
            lowered=lowered,
            child_name=child_name,
            provenance_source=provenance_source,
        )

    async def _placement_candidate_entities(
        self,
        session: AsyncSession,
        *,
        user_id: uuid.UUID,
        text: str,
        limit: int = 10,
    ) -> list[MemoryEntity]:
        lowered = normalize_text_fragment(text)
        if not lowered:
            return []
        entities = list(
            (
                await session.execute(
                    select(MemoryEntity).where(
                        MemoryEntity.user_id == user_id,
                        MemoryEntity.entity_kind != MemoryEntityKind.child,
                        MemoryEntity.entity_kind != MemoryEntityKind.routine_anchor,
                    )
                )
            )
            .scalars()
            .all()
        )
        scored = [
            (entity, _entity_candidate_score(entity, lowered))
            for entity in entities
        ]
        scored = [item for item in scored if item[1] > 0]
        scored.sort(key=lambda item: (item[1], item[0].updated_at or utc_now()), reverse=True)
        return [entity for entity, _ in scored[:limit]]

    def _graph_node(
        self,
        memory: MemoryItem,
        *,
        primary_entity: MemoryEntity | None = None,
        root_entity_id: uuid.UUID | None = None,
        root_label: str | None = None,
    ) -> MemoryGraphNode:
        semantic = self._semantic_payload_from_memory(memory)
        return MemoryGraphNode(
            id=str(memory.id),
            label=_memory_graph_label(memory),
            kind="memory",
            memory_type=memory.memory_type.value,
            memory_type_label=_memory_type_label(memory.memory_type),
            summary=_memory_display_summary(memory),
            pinned=bool(memory.pinned),
            archived=bool(memory.disabled),
            updated_at=memory.updated_at.isoformat() if memory.updated_at else None,
            world_section=self._memory_world_section(memory),
            semantic_group=(semantic.group if semantic else None),
            semantic_label=(semantic.label if semantic else None),
            semantic_relation=(semantic.relation if semantic else None),
            semantic_path=(list(semantic.path or []) if semantic else []),
            breadcrumb=self._graph_breadcrumb_for_memory(
                memory,
                primary_entity=primary_entity,
                root_entity_id=root_entity_id,
                root_label=root_label,
            ),
            icon_key=self._icon_key_for_node(
                kind="memory",
                facet=(primary_entity.default_facet.value if primary_entity is not None else None),
                memory_type=memory.memory_type.value,
            ),
            branch_label=(
                _facet_group_label(_facet_group_key(primary_entity.default_facet))
                if primary_entity is not None
                else None
            ),
        )

    def _graph_edge(self, relationship: MemoryRelationship) -> MemoryGraphEdge:
        return MemoryGraphEdge(
            id=f"structural:{relationship.id}",
            source=str(relationship.parent_memory_id),
            target=str(relationship.child_memory_id),
            kind="structural",
            relationship_type=relationship.relationship_type.value,
            label=_relationship_type_label(relationship.relationship_type),
            cascades=relationship.relationship_type in _CASCADE_RELATIONSHIP_TYPES,
        )

    def _semantic_assignments(
        self,
        memories: list[MemoryItem],
    ) -> dict[uuid.UUID, MemoryConceptAssignment]:
        candidate_rows: dict[uuid.UUID, list[MemoryConceptAssignment]] = {}
        frequencies: Counter[str] = Counter()
        for memory in memories:
            candidates = self._concept_candidates(memory)
            candidate_rows[memory.id] = candidates
            frequencies.update(candidate.key for candidate in candidates if candidate.key)

        assignments: dict[uuid.UUID, MemoryConceptAssignment] = {}
        for memory in memories:
            candidates = candidate_rows.get(memory.id, [])
            if not candidates:
                assignments[memory.id] = MemoryConceptAssignment(
                    key=None,
                    label="Memory",
                    kind="topic",
                )
                continue
            ranked = sorted(
                candidates,
                key=lambda candidate: (
                    frequencies.get(candidate.key or "", 0) + _concept_priority(candidate.key),
                    candidate.kind == "person",
                ),
                reverse=True,
            )
            assignments[memory.id] = ranked[0]
        return assignments

    def _semantic_cluster_graph(
        self,
        memories: list[MemoryItem],
        *,
        concept_assignments: dict[uuid.UUID, MemoryConceptAssignment],
        person_label: str,
    ) -> tuple[list[MemoryGraphNode], list[MemoryGraphEdge]]:
        person_node_id = f"person:{_slugify(person_label) or 'child'}"
        person_node = MemoryGraphNode(
            id=person_node_id,
            label=person_label,
            kind="person",
            memory_type="person_anchor",
            memory_type_label="Person",
            summary=f"The main relationship hub for {person_label}. Related memories branch out from here.",
            item_count=len(memories),
        )

        concept_counts = Counter(
            assignment.key
            for assignment in concept_assignments.values()
            if assignment.key
        )
        cluster_nodes: list[MemoryGraphNode] = [person_node]
        edges: list[MemoryGraphEdge] = []
        cluster_ids: dict[str, str] = {}

        for concept_key, count in concept_counts.items():
            if count < 2:
                continue
            assignment = next(
                (item for item in concept_assignments.values() if item.key == concept_key),
                None,
            )
            if assignment is None:
                continue
            cluster_id = f"cluster:{_slugify(concept_key) or uuid.uuid4().hex[:8]}"
            cluster_ids[concept_key] = cluster_id
            cluster_nodes.append(
                MemoryGraphNode(
                    id=cluster_id,
                    label=assignment.label,
                    kind=assignment.kind if assignment.kind in {"person", "topic"} else "topic",
                    memory_type="topic_cluster",
                    memory_type_label="Memory cluster",
                    summary=f"{count} memories connect around {assignment.label.lower()}.",
                    item_count=count,
                )
            )
            edges.append(
                MemoryGraphEdge(
                    id=f"anchor:{person_node_id}:{cluster_id}",
                    source=person_node_id,
                    target=cluster_id,
                    kind="structural",
                    relationship_type="person_cluster",
                    label="",
                )
            )

        for memory in memories:
            assignment = concept_assignments.get(memory.id)
            concept_key = assignment.key if assignment else None
            if concept_key and concept_key in cluster_ids:
                edges.append(
                    MemoryGraphEdge(
                        id=f"cluster-member:{cluster_ids[concept_key]}:{memory.id}",
                        source=cluster_ids[concept_key],
                        target=str(memory.id),
                        kind="structural",
                        relationship_type="topic_member",
                        label="",
                    )
                )
            else:
                edges.append(
                    MemoryGraphEdge(
                        id=f"anchor-memory:{person_node_id}:{memory.id}",
                        source=person_node_id,
                        target=str(memory.id),
                        kind="structural",
                        relationship_type="person_memory",
                        label="",
                    )
                )

        return cluster_nodes, edges

    def _similarity_edges(
        self,
        memories: list[MemoryItem],
        *,
        structural_pairs: set[frozenset[uuid.UUID]],
        per_node_limit: int,
        week_by_memory: dict[uuid.UUID, str],
    ) -> list[MemoryGraphEdge]:
        if per_node_limit <= 0:
            return []

        pair_candidates: list[tuple[float, MemoryItem, MemoryItem]] = []
        for left_index, left in enumerate(memories):
            if not _has_embedding(left.embedding_vector):
                continue
            for right in memories[left_index + 1 :]:
                if not _has_embedding(right.embedding_vector):
                    continue
                pair_key = frozenset((left.id, right.id))
                if pair_key in structural_pairs:
                    continue
                if week_by_memory.get(left.id) != week_by_memory.get(right.id):
                    continue
                score = cosine_similarity(left.embedding_vector, right.embedding_vector)
                if score < max(0.72, float(self.settings.memory.similarity_threshold)):
                    continue
                pair_candidates.append((score, left, right))

        pair_candidates.sort(key=lambda item: item[0], reverse=True)
        counts: dict[uuid.UUID, int] = {}
        edges: list[MemoryGraphEdge] = []
        for score, left, right in pair_candidates:
            if counts.get(left.id, 0) >= per_node_limit or counts.get(right.id, 0) >= per_node_limit:
                continue
            edge_id = f"similarity:{min(str(left.id), str(right.id))}:{max(str(left.id), str(right.id))}"
            edges.append(
                MemoryGraphEdge(
                    id=edge_id,
                    source=str(left.id),
                    target=str(right.id),
                    kind="similarity",
                    label=f"{score:.2f}",
                )
            )
            counts[left.id] = counts.get(left.id, 0) + 1
            counts[right.id] = counts.get(right.id, 0) + 1
        return edges

    def _cluster_similarity_edges(
        self,
        memories: list[MemoryItem],
        *,
        structural_pairs: set[frozenset[uuid.UUID]],
        concept_by_memory: dict[uuid.UUID, str | None],
        per_node_limit: int,
    ) -> list[MemoryGraphEdge]:
        if per_node_limit <= 0:
            return []

        pair_candidates: list[tuple[float, MemoryItem, MemoryItem]] = []
        for left_index, left in enumerate(memories):
            if not _has_embedding(left.embedding_vector):
                continue
            left_concept = concept_by_memory.get(left.id)
            for right in memories[left_index + 1 :]:
                if not _has_embedding(right.embedding_vector):
                    continue
                pair_key = frozenset((left.id, right.id))
                if pair_key in structural_pairs:
                    continue
                right_concept = concept_by_memory.get(right.id)
                score = cosine_similarity(left.embedding_vector, right.embedding_vector)
                if left_concept and right_concept and left_concept == right_concept:
                    if score < max(0.76, float(self.settings.memory.similarity_threshold)):
                        continue
                elif score < max(0.88, float(self.settings.memory.similarity_threshold) + 0.08):
                    continue
                pair_candidates.append((score, left, right))

        pair_candidates.sort(key=lambda item: item[0], reverse=True)
        counts: dict[uuid.UUID, int] = {}
        edges: list[MemoryGraphEdge] = []
        for score, left, right in pair_candidates:
            if counts.get(left.id, 0) >= per_node_limit or counts.get(right.id, 0) >= per_node_limit:
                continue
            edge_id = f"similarity:{min(str(left.id), str(right.id))}:{max(str(left.id), str(right.id))}"
            edges.append(
                MemoryGraphEdge(
                    id=edge_id,
                    source=str(left.id),
                    target=str(right.id),
                    kind="similarity",
                    label=f"{score:.2f}",
                )
            )
            counts[left.id] = counts.get(left.id, 0) + 1
            counts[right.id] = counts.get(right.id, 0) + 1
        return edges

    def _time_bucket_graph(
        self,
        memories: list[MemoryItem],
    ) -> tuple[list[MemoryGraphNode], list[MemoryGraphEdge], dict[uuid.UUID, str]]:
        if not memories:
            return [], [], {}

        week_nodes: dict[str, MemoryGraphNode] = {}
        day_nodes: dict[str, MemoryGraphNode] = {}
        edges: dict[str, MemoryGraphEdge] = {}
        week_counts: dict[str, int] = {}
        day_counts: dict[str, int] = {}
        week_by_memory: dict[uuid.UUID, str] = {}

        for memory in memories:
            timestamp = memory.updated_at or memory.created_at or utc_now()
            bucket_day = timestamp.date()
            week_start = bucket_day - timedelta(days=bucket_day.weekday())
            week_id = f"week:{week_start.isoformat()}"
            day_id = f"day:{bucket_day.isoformat()}"

            week_by_memory[memory.id] = week_id
            week_counts[week_id] = week_counts.get(week_id, 0) + 1
            day_counts[day_id] = day_counts.get(day_id, 0) + 1

            week_nodes.setdefault(
                week_id,
                MemoryGraphNode(
                    id=week_id,
                    label=_week_bucket_label(week_start),
                    kind="week",
                    memory_type="time_group",
                    memory_type_label="Week",
                    summary="",
                ),
            )
            day_nodes.setdefault(
                day_id,
                MemoryGraphNode(
                    id=day_id,
                    label=_day_bucket_label(bucket_day),
                    kind="day",
                    memory_type="time_group",
                    memory_type_label="Day",
                    summary="",
                ),
            )

            edges[f"time-week:{week_id}:{day_id}"] = MemoryGraphEdge(
                id=f"time-week:{week_id}:{day_id}",
                source=week_id,
                target=day_id,
                kind="structural",
                relationship_type="time_week",
                label="",
            )
            edges[f"time-day:{day_id}:{memory.id}"] = MemoryGraphEdge(
                id=f"time-day:{day_id}:{memory.id}",
                source=day_id,
                target=str(memory.id),
                kind="structural",
                relationship_type="time_day",
                label="",
            )

        for week_id, node in week_nodes.items():
            count = week_counts.get(week_id, 0)
            node.item_count = count
            node.summary = f"{count} memor{'y' if count == 1 else 'ies'} grouped into this week."

        for day_id, node in day_nodes.items():
            count = day_counts.get(day_id, 0)
            node.item_count = count
            node.summary = f"{count} memor{'y' if count == 1 else 'ies'} captured on this day."

        return (
            sorted([*week_nodes.values(), *day_nodes.values()], key=lambda item: (0 if item.kind == "week" else 1, item.id)),
            list(edges.values()),
            week_by_memory,
        )

    async def _similar_memories_for_inspector(
        self,
        session: AsyncSession,
        *,
        memory: MemoryItem,
        user_id: uuid.UUID,
        exclude_ids: set[uuid.UUID],
        limit: int = 4,
    ) -> list[MemoryLinkedMemory]:
        if not _has_embedding(memory.embedding_vector):
            return []
        candidates = list(
            (
                await session.execute(
                    select(MemoryItem)
                    .where(
                        MemoryItem.user_id == user_id,
                        MemoryItem.disabled.is_(False),
                    )
                    .order_by(desc(MemoryItem.pinned), desc(MemoryItem.importance_score), desc(MemoryItem.updated_at))
                    .limit(80)
                )
            )
            .scalars()
            .all()
        )
        scored: list[tuple[float, MemoryItem]] = []
        for candidate in candidates:
            if candidate.id in exclude_ids or candidate.id == memory.id:
                continue
            if not _has_embedding(candidate.embedding_vector):
                continue
            score = cosine_similarity(memory.embedding_vector, candidate.embedding_vector)
            if score < max(0.72, float(self.settings.memory.similarity_threshold)):
                continue
            scored.append((score, candidate))
        scored.sort(key=lambda item: item[0], reverse=True)
        return [
            MemoryLinkedMemory(
                id=str(candidate.id),
                title=_memory_display_title(candidate),
                summary=_memory_display_summary(candidate),
                kind="similarity",
                relationship_label="Similar theme",
                archived=bool(candidate.disabled),
                pinned=bool(candidate.pinned),
            )
            for _, candidate in scored[:limit]
        ]

    def _concept_candidates(self, memory: MemoryItem) -> list[MemoryConceptAssignment]:
        metadata = dict(memory.metadata_json or {})
        candidates: list[MemoryConceptAssignment] = []
        seen: set[str] = set()

        entity_name = str(metadata.get("entity_name") or "").strip()
        entity_kind = str(metadata.get("entity_kind") or "topic").strip().lower() or "topic"
        if entity_name:
            key = f"entity:{entity_kind}:{entity_name.casefold()}"
            candidates.append(
                MemoryConceptAssignment(
                    key=key,
                    label=_display_phrase(entity_name),
                    kind="person" if entity_kind in {"person", "friend", "family", "caregiver", "artist", "teacher"} else "topic",
                )
            )
            seen.add(key)

        for raw_tag in list(memory.tags or []):
            normalized_tag = _normalize_concept_tag(raw_tag)
            if not normalized_tag:
                continue
            key = f"tag:{normalized_tag.casefold()}"
            if key in seen:
                continue
            candidates.append(MemoryConceptAssignment(key=key, label=normalized_tag, kind="topic"))
            seen.add(key)

        inferred_key, inferred_label = _infer_theme(memory)
        if inferred_key and inferred_key not in seen:
            candidates.append(MemoryConceptAssignment(key=inferred_key, label=inferred_label, kind="topic"))
            seen.add(inferred_key)

        fallback_key, fallback_label = _type_cluster(memory.memory_type)
        if fallback_key not in seen:
            candidates.append(MemoryConceptAssignment(key=fallback_key, label=fallback_label, kind="topic"))

        return candidates


def cosine_similarity(left: list[float], right: list[float]) -> float:
    dot = sum(a * b for a, b in zip(left, right))
    left_norm = math.sqrt(sum(a * a for a in left))
    right_norm = math.sqrt(sum(b * b for b in right))
    if left_norm == 0 or right_norm == 0:
        return 0.0
    return dot / (left_norm * right_norm)


def _has_embedding(vector: Any) -> bool:
    if vector is None:
        return False
    try:
        return len(vector) > 0
    except TypeError:
        return True


def _week_bucket_label(bucket_day) -> str:
    return f"Week of {bucket_day.strftime('%b')} {bucket_day.day}"


def _day_bucket_label(bucket_day) -> str:
    return f"{bucket_day.strftime('%a %b')} {bucket_day.day}"


def _merge_tags(*tag_groups: list[str] | Any) -> list[str]:
    seen: list[str] = []
    for group in tag_groups:
        if not isinstance(group, list):
            continue
        for tag in group:
            value = str(tag).strip()
            if value and value not in seen:
                seen.append(value)
    return seen[:16]


def _merge_text(existing: str, new: str) -> str:
    existing_clean = existing.strip()
    new_clean = new.strip()
    if not existing_clean:
        return new_clean
    if not new_clean:
        return existing_clean
    if new_clean in existing_clean:
        return existing_clean
    return f"{existing_clean} {new_clean}".strip()


def _normalize_uuid(value: Any) -> uuid.UUID | None:
    if isinstance(value, uuid.UUID):
        return value
    text = str(value or "").strip()
    if not text:
        return None
    try:
        return uuid.UUID(text)
    except (TypeError, ValueError):
        return None


def _normalize_optional_text(value: Any) -> str | None:
    text = str(value or "").strip()
    return text or None


def _normalize_required_text(value: Any) -> str | None:
    text = str(value or "").strip()
    return text or None


def _normalize_tags(value: Any) -> list[str]:
    if isinstance(value, list):
        return _merge_tags(value)
    text = str(value or "").strip()
    if not text:
        return []
    return _merge_tags([part.strip() for part in text.split(",")])


def _coerce_bool(value: Any, *, default: bool = False) -> bool:
    if isinstance(value, bool):
        return value
    if value is None:
        return default
    text = str(value).strip().lower()
    if text in {"1", "true", "yes", "on"}:
        return True
    if text in {"0", "false", "no", "off"}:
        return False
    return default


def _memory_type_label(memory_type: MemoryType | str) -> str:
    return str(memory_type).replace("_", " ").strip().title()


def _memory_display_title(memory: MemoryItem | None) -> str:
    if memory is None:
        return "Memory"
    title = (memory.title or "").strip()
    if title:
        return title
    summary = (memory.summary or "").strip()
    if summary:
        return summary[:72]
    return (memory.content or "").strip()[:72] or "Memory"


def _memory_display_summary(memory: MemoryItem) -> str:
    summary = (memory.summary or "").strip()
    if summary:
        return summary
    content = (memory.content or "").strip()
    if len(content) <= 180:
        return content
    return f"{content[:177].rstrip()}..."


def _memory_change_source_label(metadata: dict[str, Any]) -> str | None:
    source_kind = str(metadata.get("source_kind") or metadata.get("source") or "").strip().lower()
    if source_kind in {"parent_guidance", "parent_portal_chat"}:
        return "Parent chat"
    if source_kind in {"sms", "message"}:
        return "SMS"
    if source_kind in {"voice", "call"}:
        return "Voice"
    if source_kind == "daily_life":
        return "Daily routine"
    if source_kind == "consolidation":
        return "Consolidation"
    return None


def _memory_graph_label(memory: MemoryItem) -> str:
    title = _memory_display_title(memory)
    if len(title) <= 36:
        return title
    return f"{title[:33].rstrip()}..."


def _person_anchor_label(user: User | None) -> str:
    if user is None:
        return "Person"
    label = str(user.display_name or "").strip()
    return label or "Person"


def _concept_priority(key: str | None) -> float:
    normalized = str(key or "")
    if normalized.startswith("entity:"):
        return 1.6
    if normalized.startswith("tag:"):
        return 1.2
    if normalized.startswith("theme:"):
        return 0.9
    if normalized.startswith("type:"):
        return 0.35
    return 0.0


def _display_phrase(value: str) -> str:
    cleaned = re.sub(r"[_-]+", " ", str(value or "").strip())
    if not cleaned:
        return "Topic"
    return " ".join(part.capitalize() if part.islower() else part for part in cleaned.split())


def _slugify(value: str) -> str:
    normalized = re.sub(r"[^a-z0-9]+", "-", str(value or "").casefold()).strip("-")
    return normalized[:80]


def _normalize_concept_tag(value: str) -> str | None:
    cleaned = re.sub(r"[_-]+", " ", str(value or "").strip())
    normalized = cleaned.casefold()
    if not normalized:
        return None
    if normalized in {
        "parent guidance",
        "preference",
        "guidance",
        "memory",
        "operator note",
        "summary",
        "fact",
        "episode",
        "follow up",
        "followup",
    }:
        return None
    if any(marker in normalized for marker in _ROUTINE_TEXT_MARKERS):
        return None
    if len(normalized) < 3:
        return None
    return _display_phrase(cleaned)


def _infer_theme(memory: MemoryItem) -> tuple[str | None, str]:
    haystack = " ".join(
        part
        for part in [
            memory.title or "",
            memory.summary or "",
            memory.content or "",
            " ".join(list(memory.tags or [])),
        ]
        if part
    ).casefold()
    if not haystack:
        return None, "Topic"

    themes = {
        "theme:music": ("Music", ("music", "song", "songs", "sing", "singing", "playlist", "album", "artist", "band")),
        "theme:school": ("School", ("school", "teacher", "class", "homework", "bus", "classroom")),
        "theme:family": ("Family", ("mom", "mother", "dad", "father", "sister", "brother", "grandma", "grandpa", "family")),
        "theme:friends": ("Friends", ("friend", "friends", "bestie", "buddy")),
        "theme:creativity": ("Creativity", ("draw", "drawing", "art", "paint", "lego", "craft", "game", "gaming")),
        "theme:comfort": ("Comfort", ("calm", "comfort", "safe", "reassur", "worried", "anxious", "overwhelmed", "upset", "stress")),
        "theme:food": ("Food", ("food", "snack", "eat", "lunch", "dinner", "breakfast")),
    }
    for key, (label, markers) in themes.items():
        if any(marker in haystack for marker in markers):
            return key, label
    return None, "Topic"


def _type_cluster(memory_type: MemoryType) -> tuple[str, str]:
    mapping = {
        MemoryType.preference: ("type:preferences", "Preferences"),
        MemoryType.safety: ("type:safety", "Safety"),
        MemoryType.summary: ("type:summaries", "Summaries"),
        MemoryType.operator_note: ("type:guidance", "Parent Guidance"),
        MemoryType.follow_up: ("type:follow-ups", "Follow-Ups"),
        MemoryType.episode: ("type:shared-moments", "Shared Moments"),
        MemoryType.fact: ("type:key-details", "Key Details"),
    }
    return mapping.get(memory_type, ("type:memories", "Memories"))


_ROUTINE_TEXT_MARKERS = (
    "daily",
    "routine",
    "morning",
    "evening",
    "bedtime",
    "wake up",
    "wakeup",
    "after school",
    "after-school",
    "homework",
    "quiet hours",
    "getting ready",
    "wind down",
    "check in",
    "check-in",
    "schedule",
    "cadence",
    "transition",
    "school night",
    "meal",
    "meals",
    "snack",
    "snacks",
    "eating",
    "breakfast",
    "lunch",
    "dinner",
    "bath",
    "brushing teeth",
    "toothbrush",
    "plan ",
    "plans ",
)


def _is_daily_routine_memory(memory: MemoryItem) -> bool:
    metadata = dict(memory.metadata_json or {})
    if str(metadata.get("source_kind") or "").strip().casefold() in {"call_follow_up"}:
        return True
    if str(metadata.get("source") or "").strip().casefold() in {"daily_life"}:
        return True

    tag_text = " ".join(str(tag or "").strip() for tag in list(memory.tags or [])).casefold()
    haystack = " ".join(
        part
        for part in [
            memory.title or "",
            memory.summary or "",
            memory.content or "",
            tag_text,
            str(metadata.get("source") or ""),
            str(metadata.get("slot") or ""),
            str(metadata.get("entity_name") or ""),
            str(metadata.get("entity_kind") or ""),
        ]
        if part
    ).casefold()
    if any(marker in haystack for marker in _ROUTINE_TEXT_MARKERS):
        return True

    if memory.memory_type == MemoryType.follow_up and any(
        marker in haystack
        for marker in ("later today", "tomorrow", "tonight", "again later", "check in", "follow up")
    ):
        return True
    return False


def normalize_text_fragment(value: str) -> str:
    return re.sub(r"\s+", " ", str(value or "").strip()).casefold()


def _coerce_entity_kind(raw: str) -> MemoryEntityKind:
    normalized = normalize_text_fragment(raw).replace(" ", "_")
    aliases = {
        "family": MemoryEntityKind.family_member,
        "family_member": MemoryEntityKind.family_member,
        "person": MemoryEntityKind.friend,
        "friend": MemoryEntityKind.friend,
        "pet": MemoryEntityKind.pet,
        "artist": MemoryEntityKind.artist,
        "activity": MemoryEntityKind.activity,
        "routine": MemoryEntityKind.routine_anchor,
        "routine_anchor": MemoryEntityKind.routine_anchor,
        "event": MemoryEntityKind.event,
        "health": MemoryEntityKind.health_context,
        "health_context": MemoryEntityKind.health_context,
        "topic": MemoryEntityKind.topic,
        "child": MemoryEntityKind.child,
    }
    return aliases.get(normalized, MemoryEntityKind.topic)


def _coerce_memory_facet(raw: str) -> MemoryFacet | None:
    normalized = normalize_text_fragment(raw).replace(" ", "_")
    if not normalized:
        return None
    aliases = {
        facet.value: facet
        for facet in MemoryFacet
    }
    return aliases.get(normalized)


def _default_facet_for_entity_kind(entity_kind: MemoryEntityKind) -> MemoryFacet:
    mapping = {
        MemoryEntityKind.child: MemoryFacet.identity,
        MemoryEntityKind.family_member: MemoryFacet.family,
        MemoryEntityKind.friend: MemoryFacet.friends,
        MemoryEntityKind.pet: MemoryFacet.pets,
        MemoryEntityKind.artist: MemoryFacet.favorites,
        MemoryEntityKind.activity: MemoryFacet.interests,
        MemoryEntityKind.routine_anchor: MemoryFacet.routines,
        MemoryEntityKind.event: MemoryFacet.events,
        MemoryEntityKind.health_context: MemoryFacet.health_context,
        MemoryEntityKind.topic: MemoryFacet.preferences,
    }
    return mapping.get(entity_kind, MemoryFacet.events)


def _default_relation_for_entity_kind(entity_kind: MemoryEntityKind) -> EntityRelationKind:
    mapping = {
        MemoryEntityKind.child: EntityRelationKind.child_world,
        MemoryEntityKind.family_member: EntityRelationKind.family_member,
        MemoryEntityKind.friend: EntityRelationKind.friend,
        MemoryEntityKind.pet: EntityRelationKind.pet,
        MemoryEntityKind.artist: EntityRelationKind.favorite,
        MemoryEntityKind.activity: EntityRelationKind.interest,
        MemoryEntityKind.routine_anchor: EntityRelationKind.routine,
        MemoryEntityKind.event: EntityRelationKind.related,
        MemoryEntityKind.health_context: EntityRelationKind.related,
        MemoryEntityKind.topic: EntityRelationKind.related,
    }
    return mapping.get(entity_kind, EntityRelationKind.related)


def _coerce_relation_kind(raw: str | None, *, entity_kind: MemoryEntityKind) -> EntityRelationKind:
    normalized = normalize_text_fragment(raw or "").replace(" ", "_")
    aliases = {
        EntityRelationKind.child_world.value: EntityRelationKind.child_world,
        EntityRelationKind.family_member.value: EntityRelationKind.family_member,
        EntityRelationKind.friend.value: EntityRelationKind.friend,
        EntityRelationKind.pet.value: EntityRelationKind.pet,
        EntityRelationKind.favorite.value: EntityRelationKind.favorite,
        EntityRelationKind.interest.value: EntityRelationKind.interest,
        EntityRelationKind.routine.value: EntityRelationKind.routine,
        EntityRelationKind.related.value: EntityRelationKind.related,
    }
    return aliases.get(normalized, _default_relation_for_entity_kind(entity_kind))


def _facet_group_key(facet: MemoryFacet | str) -> str:
    value = facet.value if isinstance(facet, MemoryFacet) else str(facet or "").strip().lower()
    if value in {MemoryFacet.favorites.value, MemoryFacet.preferences.value, MemoryFacet.interests.value}:
        return "likes_and_preferences"
    if value in {MemoryFacet.family.value, MemoryFacet.friends.value, MemoryFacet.pets.value}:
        return value
    if value == MemoryFacet.identity.value:
        return "identity"
    if value == MemoryFacet.milestones.value:
        return "milestones"
    if value == MemoryFacet.health_context.value:
        return "health_context"
    if value == MemoryFacet.events.value:
        return "events"
    return value or "identity"


def _facet_group_label(facet_key: str) -> str:
    labels = {
        "identity": "Profile",
        "family": "Family",
        "friends": "Friends",
        "pets": "Pets",
        "likes_and_preferences": "Likes and Preferences",
        "milestones": "Milestones",
        "health_context": "Health Context",
        "events": "Important Events",
    }
    return labels.get(facet_key, str(facet_key or "Profile").replace("_", " ").title())


def _facet_group_summary(facet_key: str) -> str:
    summaries = {
        "identity": "The core facts Resona keeps about the child.",
        "family": "Family members and how they fit into the child's world.",
        "friends": "Friends and important social connections.",
        "pets": "Pets and animal companions that matter day to day.",
        "likes_and_preferences": "Favorites, interests, and the things that feel familiar or comforting.",
        "milestones": "Important dates, transitions, and personal milestones.",
        "health_context": "Relevant health context that helps Resona respond appropriately.",
        "events": "Notable events and moments that may matter later.",
    }
    return summaries.get(facet_key, "A grouped part of the child's memory world.")


def _infer_memory_facet(memory: MemoryItem, lowered: str, *, default: MemoryFacet) -> MemoryFacet:
    if any(token in lowered for token in ("favorite", "loves", "likes", "enjoys", "prefers")):
        return MemoryFacet.favorites if "favorite" in lowered else MemoryFacet.preferences
    if any(token in lowered for token in ("routine", "after school", "bedtime", "morning", "evening", "daily")):
        return MemoryFacet.routines
    if any(token in lowered for token in ("birthday", "bday", "turning", "years old", "born")):
        return MemoryFacet.milestones
    if any(token in lowered for token in ("syndrome", "therapy", "speech", "medical")):
        return MemoryFacet.health_context
    if memory.memory_type == MemoryType.preference:
        return MemoryFacet.preferences
    return default


def _infer_family_relation(content: str, title: str) -> str | None:
    lowered = normalize_text_fragment(f"{title} {content}")
    for relation in ("brother", "sister", "mother", "mom", "father", "dad"):
        if relation in lowered:
            return relation
    return None


def _extract_named_person(content: str, title: str) -> str | None:
    patterns = (
        re.compile(r"(?:is|named)\s+([A-Z][a-z]+(?:\s+[A-Z][a-z]+)*)"),
        re.compile(r"^([A-Z][a-z]+(?:\s+[A-Z][a-z]+)*)$"),
    )
    for source in (title, content):
        for pattern in patterns:
            match = pattern.search(source or "")
            if match:
                return match.group(1).strip()
    return None


def _split_named_values_from_text(value: str) -> list[str]:
    cleaned = re.sub(r"[^A-Za-z,& ]+", " ", str(value or "")).strip()
    if not cleaned:
        return []
    parts = re.split(r"\s*(?:,| and | & )\s*", cleaned)
    names: list[str] = []
    for part in parts:
        candidate = " ".join(piece.capitalize() for piece in part.split())
        if candidate and candidate not in names:
            names.append(candidate)
    return names


def _infer_pet_name(content: str, title: str, *, entity_name: str) -> str | None:
    if entity_name:
        return entity_name
    patterns = (
        re.compile(r"(?:kitten|cat|dog|pet)'s name is ([A-Z][a-z]+)", flags=re.IGNORECASE),
        re.compile(r"named ([A-Z][a-z]+)", flags=re.IGNORECASE),
    )
    for source in (content, title):
        for pattern in patterns:
            match = pattern.search(source or "")
            if match:
                return match.group(1).strip().title()
    return None


def _infer_pet_relation(content: str, title: str) -> str:
    lowered = normalize_text_fragment(f"{title} {content}")
    if "kitten" in lowered:
        return "kitten"
    if "dog" in lowered:
        return "dog"
    if "cat" in lowered:
        return "cat"
    return "pet"


def _routine_anchor_name(content: str, *, child_name: str) -> str:
    lowered = normalize_text_fragment(content)
    if "morning" in lowered:
        return "Morning routine"
    if "after school" in lowered:
        return "After-school routine"
    if "bedtime" in lowered:
        return "Bedtime routine"
    if "evening" in lowered:
        return "Evening routine"
    return f"{child_name}'s daily routine"


def _topic_entity_name(memory: MemoryItem) -> str | None:
    metadata = dict(memory.metadata_json or {})
    entity_name = str(metadata.get("structured_topic_name") or "").strip()
    if entity_name:
        return entity_name
    tags = [str(tag or "").strip() for tag in list(memory.tags or []) if str(tag or "").strip()]
    generic = {"parent-guidance", "fact", "preference", "friends", "family", "name"}
    for tag in tags:
        normalized = normalize_text_fragment(tag)
        if normalized and normalized not in generic:
            return tag.replace("-", " ").title()
    theme_key, theme_label = _infer_theme(memory)
    if theme_key and not theme_key.startswith("type:"):
        return theme_label
    return None


def _compat_entity_kind(value: str | None) -> MemoryEntityKind:
    normalized = normalize_text_fragment(value or "")
    for kind in MemoryEntityKind:
        if normalized == kind.value:
            return kind
    if normalized in {"person", "sibling", "parent", "mom", "dad", "brother", "sister", "guardian"}:
        return MemoryEntityKind.family_member
    if normalized in {"friend", "peer", "classmate"}:
        return MemoryEntityKind.friend
    if normalized in {"pet", "animal", "cat", "dog"}:
        return MemoryEntityKind.pet
    if normalized in {"music", "artist", "band", "singer"}:
        return MemoryEntityKind.artist
    if normalized in {"routine", "schedule", "cadence"}:
        return MemoryEntityKind.routine_anchor
    if normalized in {"event", "birthday", "holiday", "trip"}:
        return MemoryEntityKind.event
    if normalized in {"health", "medical", "diagnosis"}:
        return MemoryEntityKind.health_context
    if normalized in {"activity", "hobby", "interest"}:
        return MemoryEntityKind.activity
    return MemoryEntityKind.topic


def _compat_memory_facet(value: str | None) -> MemoryFacet:
    normalized = normalize_text_fragment(value or "")
    for facet in MemoryFacet:
        if normalized == facet.value:
            return facet
    if normalized in {"family", "family member"}:
        return MemoryFacet.family
    if normalized in {"friend", "friends"}:
        return MemoryFacet.friends
    if normalized in {"pet", "pets"}:
        return MemoryFacet.pets
    if normalized in {"favorite", "favorites"}:
        return MemoryFacet.favorites
    if normalized in {"interest", "interests", "hobby", "activity"}:
        return MemoryFacet.interests
    if normalized in {"routine", "daily routine", "schedule"}:
        return MemoryFacet.routines
    if normalized in {"health", "health context"}:
        return MemoryFacet.health_context
    if normalized in {"event", "events", "milestone", "milestones"}:
        return MemoryFacet.events
    if normalized in {"preference", "preferences", "likes"}:
        return MemoryFacet.preferences
    return MemoryFacet.identity


def _compat_relation_kind(value: str | None) -> EntityRelationKind:
    normalized = normalize_text_fragment(value or "")
    for relation in EntityRelationKind:
        if normalized == relation.value:
            return relation
    if normalized in {"family", "family member", "sibling", "parent"}:
        return EntityRelationKind.family_member
    if normalized in {"friend", "best friend"}:
        return EntityRelationKind.friend
    if normalized in {"pet", "animal"}:
        return EntityRelationKind.pet
    if normalized in {"favorite", "likes", "favorite thing"}:
        return EntityRelationKind.favorite
    if normalized in {"interest", "hobby", "activity"}:
        return EntityRelationKind.interest
    if normalized in {"routine", "schedule"}:
        return EntityRelationKind.routine
    if normalized in {"child", "child world"}:
        return EntityRelationKind.child_world
    return EntityRelationKind.related


def _placement_from_ai_draft(
    draft: Any,
    *,
    memory: MemoryItem,
    lowered: str,
    child_name: str,
    provenance_source: str,
) -> StructuredPlacement | None:
    primary_kind = _coerce_entity_kind(str(getattr(draft, "primary_kind", "") or "child"))
    primary_name = _normalize_optional_text(getattr(draft, "primary_name", None))
    if primary_name and primary_name.casefold() == child_name.casefold():
        primary_name = None
        primary_kind = MemoryEntityKind.child
    facet = _coerce_memory_facet(str(getattr(draft, "facet", "") or "")) or _infer_memory_facet(
        memory,
        lowered,
        default=_default_facet_for_entity_kind(primary_kind),
    )
    relation_to_child = _normalize_optional_text(getattr(draft, "relation_to_child", None))
    canonical_value = _normalize_optional_text(getattr(draft, "canonical_value", None))
    related_entities: list[StructuredRelatedEntitySpec] = []
    for related in list(getattr(draft, "related_entities", []) or []):
        display_name = _normalize_optional_text(getattr(related, "display_name", None))
        if not display_name:
            continue
        entity_kind = _coerce_entity_kind(str(getattr(related, "entity_kind", "") or "topic"))
        related_entities.append(
            StructuredRelatedEntitySpec(
                display_name=display_name,
                entity_kind=entity_kind,
                relation_kind=_coerce_relation_kind(getattr(related, "relation_kind", None), entity_kind=entity_kind),
                relation_to_child=_normalize_optional_text(getattr(related, "relation_to_child", None)),
                facet=_coerce_memory_facet(str(getattr(related, "facet", "") or "")) or _default_facet_for_entity_kind(entity_kind),
                canonical_value=_normalize_optional_text(getattr(related, "canonical_value", None)) or display_name,
            )
        )

    return StructuredPlacement(
        primary_name=primary_name if primary_kind != MemoryEntityKind.child else None,
        primary_kind=primary_kind,
        facet=facet,
        relation_to_child=relation_to_child,
        relation_kind=_coerce_relation_kind(getattr(draft, "relation_kind", None), entity_kind=primary_kind),
        canonical_value=canonical_value or _memory_display_summary(memory),
        provenance_source=provenance_source,
        related_entities=related_entities,
    )


def _related_entities_from_payload(value: Any) -> list[StructuredRelatedEntitySpec]:
    if not isinstance(value, list):
        return []
    related_entities: list[StructuredRelatedEntitySpec] = []
    seen: set[tuple[str, str]] = set()
    for item in value[:6]:
        if not isinstance(item, dict):
            continue
        display_name = _normalize_optional_text(item.get("display_name"))
        entity_kind_raw = str(item.get("entity_kind") or "").strip()
        if not display_name or not entity_kind_raw:
            continue
        entity_kind = _coerce_entity_kind(entity_kind_raw)
        key = (display_name.casefold(), entity_kind.value)
        if key in seen:
            continue
        seen.add(key)
        related_entities.append(
            StructuredRelatedEntitySpec(
                display_name=display_name,
                entity_kind=entity_kind,
                relation_kind=_coerce_relation_kind(item.get("relation_kind"), entity_kind=entity_kind),
                relation_to_child=_normalize_optional_text(item.get("relation_to_child")),
                facet=_coerce_memory_facet(str(item.get("facet") or "")) or _default_facet_for_entity_kind(entity_kind),
                canonical_value=_normalize_optional_text(item.get("canonical_value")) or display_name,
            )
        )
    return related_entities


def _entity_candidate_score(entity: MemoryEntity, lowered: str) -> float:
    name = normalize_text_fragment(entity.display_name)
    if not name:
        return 0.0
    score = 0.0
    if name in lowered:
        score += 3.0
    tokens = [token for token in name.split() if len(token) >= 3]
    token_matches = sum(1 for token in tokens if token in lowered)
    score += float(token_matches)
    relation = normalize_text_fragment(entity.relation_to_child or "")
    if relation and relation in lowered:
        score += 0.75
    canonical = normalize_text_fragment(entity.canonical_value or "")
    if canonical and canonical in lowered:
        score += 0.5
    if len(tokens) == 1 and token_matches == 1:
        score += 0.25
    return score


def _best_existing_entity_match(
    *,
    placement: StructuredPlacement,
    existing_entities: list[MemoryEntity],
    lowered: str,
) -> MemoryEntity | None:
    if placement.facet in {MemoryFacet.routines, MemoryFacet.events}:
        return None
    scored = [
        (entity, _entity_candidate_score(entity, lowered))
        for entity in existing_entities
    ]
    scored = [item for item in scored if item[1] >= 1.0]
    if not scored:
        return None
    scored.sort(key=lambda item: item[1], reverse=True)
    best_entity, best_score = scored[0]
    if len(scored) > 1 and best_score <= scored[1][1]:
        return None
    return best_entity


def _refine_placement_with_existing_entities(
    placement: StructuredPlacement,
    *,
    memory: MemoryItem,
    lowered: str,
    existing_entities: list[MemoryEntity],
    child_name: str,
) -> StructuredPlacement:
    best_match = _best_existing_entity_match(
        placement=placement,
        existing_entities=existing_entities,
        lowered=lowered,
    )
    if best_match is None:
        return placement
    if placement.primary_name and placement.primary_name.casefold() == best_match.display_name.casefold():
        return placement
    if placement.primary_kind not in {MemoryEntityKind.child, MemoryEntityKind.topic, MemoryEntityKind.event} and placement.primary_name:
        return placement

    refined = StructuredPlacement(
        primary_name=best_match.display_name,
        primary_kind=best_match.entity_kind,
        facet=placement.facet if placement.facet != MemoryFacet.identity else best_match.default_facet,
        relation_to_child=best_match.relation_to_child or placement.relation_to_child,
        relation_kind=_default_relation_for_entity_kind(best_match.entity_kind),
        canonical_value=placement.canonical_value,
        provenance_source=placement.provenance_source,
        role=placement.role,
        confidence=placement.confidence,
        related_entities=list(placement.related_entities or []),
    )
    if not refined.related_entities:
        topic_hint = _supporting_topic_for_memory(memory, lowered=lowered, child_name=child_name, primary_name=best_match.display_name)
        if topic_hint is not None:
            refined.related_entities.append(topic_hint)
    return refined


def _supporting_topic_for_memory(
    memory: MemoryItem,
    *,
    lowered: str,
    child_name: str,
    primary_name: str,
) -> StructuredRelatedEntitySpec | None:
    patterns = (
        re.compile(rf"{re.escape(normalize_text_fragment(child_name))}\s+s?\s*favorite\s+[^.]*?\s+is\s+([a-z0-9][a-z0-9 ':-]+)", flags=re.IGNORECASE),
        re.compile(r"favorite\s+[^.]*?\s+is\s+([a-z0-9][a-z0-9 ':-]+)", flags=re.IGNORECASE),
        re.compile(r"name\s+is\s+([a-z0-9][a-z0-9 ':-]+)", flags=re.IGNORECASE),
    )
    source = normalize_text_fragment(" ".join([memory.title or "", memory.summary or "", memory.content or ""]))
    for pattern in patterns:
        match = pattern.search(source)
        if match is None:
            continue
        candidate = _memory_title_label(match.group(1))
        if not candidate or candidate.casefold() == primary_name.casefold():
            continue
        return StructuredRelatedEntitySpec(
            display_name=candidate,
            entity_kind=MemoryEntityKind.topic,
            relation_kind=EntityRelationKind.favorite,
            facet=MemoryFacet.favorites,
            canonical_value=candidate,
        )
    return None


def _memory_title_label(value: str) -> str:
    cleaned = str(value or "").strip(" \t\r\n,;:\"'")
    if not cleaned:
        return ""
    return cleaned.title()


def _relationship_type_label(relationship_type: MemoryRelationshipType) -> str:
    labels = {
        MemoryRelationshipType.manual_child: "Child memory",
        MemoryRelationshipType.consolidated_into: "Consolidated memory",
        MemoryRelationshipType.supersedes: "Replaced memory",
    }
    return labels.get(relationship_type, relationship_type.value.replace("_", " ").title())


def _relationship_label(relationship: MemoryRelationship, *, focus_memory_id: uuid.UUID) -> str:
    if relationship.relationship_type == MemoryRelationshipType.manual_child:
        return "Parent memory" if relationship.child_memory_id == focus_memory_id else "Child memory"
    if relationship.relationship_type == MemoryRelationshipType.consolidated_into:
        return "Consolidated into this memory" if relationship.child_memory_id == focus_memory_id else "Built from this memory"
    if relationship.relationship_type == MemoryRelationshipType.supersedes:
        return "Supersedes this earlier memory" if relationship.child_memory_id == focus_memory_id else "Superseded by this newer memory"
    return _relationship_type_label(relationship.relationship_type)


def _cascade_reason(relationship_type: MemoryRelationshipType, *, parent_title: str) -> str:
    if relationship_type == MemoryRelationshipType.consolidated_into:
        return f"Only connected through {parent_title} as a consolidated memory"
    if relationship_type == MemoryRelationshipType.manual_child:
        return f"Only connected through {parent_title} as a child memory"
    return f"Only connected through {parent_title}"
