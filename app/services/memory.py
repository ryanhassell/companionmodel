from __future__ import annotations

import math
import uuid
from dataclasses import dataclass
from typing import Any

from sqlalchemy import delete, desc, or_, select, text
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.logging import get_logger
from app.core.settings import RuntimeSettings
from app.models.communication import Message
from app.models.enums import Direction, MemoryRelationshipType, MemoryType
from app.models.memory import MemoryItem, MemoryRelationship
from app.models.persona import Persona
from app.schemas.site import (
    MemoryDeletePreview,
    MemoryDeletePreviewEntry,
    MemoryGraphEdge,
    MemoryGraphNode,
    MemoryInspector,
    MemoryLinkedMemory,
)
from app.models.user import User
from app.providers.openai import OpenAIProvider
from app.services.prompt import PromptService
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


class MemoryService:
    def __init__(
        self,
        settings: RuntimeSettings,
        openai_provider: OpenAIProvider,
        prompt_service: PromptService,
    ) -> None:
        self.settings = settings
        self.openai_provider = openai_provider
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
        context = {
            "user": user,
            "persona": persona,
            "message": message,
            "recent_messages": recent_messages,
            "config": config,
        }
        rendered = await self.prompt_service.render(session, "memory_extraction", context)

        facts: list[dict[str, Any]] = []
        if self.openai_provider.enabled:
            response = await self.openai_provider.generate_json(
                instructions="Return JSON only.",
                input_items=[{"role": "user", "content": rendered}],
                max_output_tokens=self.settings.openai.memory_max_output_tokens,
            )
            if isinstance(response, list):
                facts = [item for item in response if isinstance(item, dict)]
            elif isinstance(response, dict) and isinstance(response.get("facts"), list):
                facts = [item for item in response["facts"] if isinstance(item, dict)]
        if not facts:
            facts = self._heuristic_facts(message.body)

        created: list[MemoryItem] = []
        for item in facts[: int(config["memory"]["max_facts_per_extraction"])]:
            content = str(item.get("content", "")).strip()
            if not content:
                continue
            memory = await self._build_or_merge_memory_item(
                session,
                user=user,
                persona=persona,
                source_message=message,
                raw_item=item,
                config=config,
            )
            if memory is None:
                continue
            created.append(memory)
        await session.flush()
        if created:
            await self.embed_items(session, created, config=config)
            await self.sync_relationships_for_user(session, user_id=user.id)
            for memory in created:
                logger.info(
                    "memory_saved",
                    memory_id=str(memory.id),
                    user_id=str(memory.user_id) if memory.user_id else None,
                    persona_id=str(memory.persona_id) if memory.persona_id else None,
                    memory_type=memory.memory_type.value,
                    title=memory.title,
                    summary=memory.summary,
                    tags=memory.tags,
                    source=(memory.metadata_json or {}).get("source"),
                    entity_name=(memory.metadata_json or {}).get("entity_name"),
                    entity_kind=(memory.metadata_json or {}).get("entity_kind"),
                    merge_count=(memory.metadata_json or {}).get("merge_count"),
                )
        return created

    async def embed_items(
        self,
        session: AsyncSession,
        items: list[MemoryItem],
        *,
        config: dict[str, Any],
    ) -> None:
        if not items or not self.openai_provider.enabled:
            return
        texts = [self._embedding_text(item) for item in items]
        embeddings = await self.openai_provider.embed_texts(texts)
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
        stmt = (
            select(MemoryItem)
            .where(
                MemoryItem.user_id == user_id,
                MemoryItem.disabled.is_(False),
            )
            .order_by(desc(MemoryItem.pinned), desc(MemoryItem.importance_score), desc(MemoryItem.created_at))
            .limit(top_k * 3)
        )
        items = list((await session.execute(stmt)).scalars().all())
        if persona_id:
            items = [item for item in items if item.persona_id in (None, persona_id)]
        if not items:
            return []
        if not self.openai_provider.enabled:
            return [
                RetrievedMemory(memory=item, score=item.importance_score, explanation="fallback_rank")
                for item in items[:top_k]
            ]
        embedding = (await self.openai_provider.embed_texts([query]))[0]
        dialect_name = session.bind.dialect.name if session.bind is not None else ""
        if dialect_name == "postgresql":
            results = await self._retrieve_postgres(
                session,
                user_id=user_id,
                persona_id=persona_id,
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
        if self.openai_provider.enabled:
            fake_user = type("SummaryUser", (), {"id": user_id})()
            context = {"transcript": transcript, "config": config, "user": fake_user, "persona": None}
            rendered = await self.prompt_service.render(session, "summarization", context)
            response = await self.openai_provider.generate_text(
                instructions="Summarize concisely for long-term memory.",
                input_items=[{"role": "user", "content": rendered}],
                max_output_tokens=self.settings.openai.memory_max_output_tokens,
            )
            summary_text = response.text or summary_text
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
        return 1

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

    async def graph_snapshot(
        self,
        session: AsyncSession,
        *,
        user_id: uuid.UUID | str,
        include_archived: bool = False,
        limit: int = 60,
        similarity_limit: int = 2,
    ) -> MemoryGraphResult:
        normalized_user_id = _normalize_uuid(user_id)
        if normalized_user_id is None:
            return MemoryGraphResult(nodes=[], structural_edges=[], similarity_edges=[])

        await self.sync_relationships_for_user(session, user_id=normalized_user_id)
        memories = await self.list_memories_for_user(
            session,
            user_id=normalized_user_id,
            include_archived=include_archived,
            limit=limit,
        )
        if not memories:
            return MemoryGraphResult(nodes=[], structural_edges=[], similarity_edges=[])

        memory_ids = [memory.id for memory in memories]
        by_id = {memory.id: memory for memory in memories}
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

        nodes = [self._graph_node(memory) for memory in memories]
        structural_edges = [self._graph_edge(row) for row in structural_rows]
        structural_pairs = {
            frozenset((row.parent_memory_id, row.child_memory_id))
            for row in structural_rows
        }
        similarity_edges = self._similarity_edges(
            memories,
            structural_pairs=structural_pairs,
            per_node_limit=similarity_limit,
        )
        return MemoryGraphResult(
            nodes=nodes,
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
            linked_memories=linked_memories,
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

        await session.flush()
        if text_changed:
            await self.embed_items(session, [memory], config=config)
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
        return preview

    def _embedding_text(self, item: MemoryItem) -> str:
        tags = ", ".join(item.tags or [])
        metadata = item.metadata_json or {}
        entity_name = metadata.get("entity_name") or ""
        entity_kind = metadata.get("entity_kind") or ""
        return (
            f"type={item.memory_type.value}\n"
            f"title={item.title or ''}\n"
            f"entity_name={entity_name}\n"
            f"entity_kind={entity_kind}\n"
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
        if not self.openai_provider.enabled:
            return {
                "same_entity": True,
                "title": existing.title or raw_item.get("title"),
                "content": _merge_text(existing.content, str(raw_item.get("content", ""))),
                "summary": raw_item.get("summary") or existing.summary,
                "tags": raw_item.get("tags", []),
                "importance_score": raw_item.get("importance_score", 0.5),
            }
        response = await self.openai_provider.generate_json(
            instructions="Return JSON only.",
            input_items=[
                {
                    "role": "user",
                    "content": (
                        "Decide whether this new memory candidate is about the same entity as the existing entity memory, "
                        "and if so merge it into an updated compact profile.\n"
                        "Return JSON only in this format: "
                        "{\"same_entity\": true/false, \"title\": \"...\", \"content\": \"...\", "
                        "\"summary\": \"...\", \"tags\": [\"...\"], \"importance_score\": 0.0}.\n"
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
                }
            ],
            max_output_tokens=self.settings.openai.memory_max_output_tokens,
        )
        if isinstance(response, dict):
            return response
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

    def _graph_node(self, memory: MemoryItem) -> MemoryGraphNode:
        return MemoryGraphNode(
            id=str(memory.id),
            label=_memory_graph_label(memory),
            memory_type=memory.memory_type.value,
            memory_type_label=_memory_type_label(memory.memory_type),
            summary=_memory_display_summary(memory),
            pinned=bool(memory.pinned),
            archived=bool(memory.disabled),
            updated_at=memory.updated_at.isoformat() if memory.updated_at else None,
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

    def _similarity_edges(
        self,
        memories: list[MemoryItem],
        *,
        structural_pairs: set[frozenset[uuid.UUID]],
        per_node_limit: int,
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


def _memory_graph_label(memory: MemoryItem) -> str:
    title = _memory_display_title(memory)
    if len(title) <= 36:
        return title
    return f"{title[:33].rstrip()}..."


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
