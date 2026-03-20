from __future__ import annotations

import math
from dataclasses import dataclass
from typing import Any

from sqlalchemy import desc, select, text
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.logging import get_logger
from app.core.settings import RuntimeSettings
from app.models.communication import Message
from app.models.enums import Direction, MemoryType
from app.models.memory import MemoryItem
from app.models.persona import Persona
from app.models.user import User
from app.providers.openai import OpenAIProvider
from app.services.prompt import PromptService
from app.utils.time import utc_now

logger = get_logger(__name__)


@dataclass(slots=True)
class RetrievedMemory:
    memory: MemoryItem
    score: float
    explanation: str


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
            return await self._retrieve_postgres(
                session,
                user_id=user_id,
                persona_id=persona_id,
                query_embedding=embedding,
                top_k=top_k,
                threshold=threshold,
            )
        return self._retrieve_python(items, embedding, top_k=top_k, threshold=threshold)

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
            if not item.embedding_vector:
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
        return 1

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
        metadata_json = {"source": "extraction"}
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


def cosine_similarity(left: list[float], right: list[float]) -> float:
    dot = sum(a * b for a, b in zip(left, right))
    left_norm = math.sqrt(sum(a * a for a in left))
    right_norm = math.sqrt(sum(b * b for b in right))
    if left_norm == 0 or right_norm == 0:
        return 0.0
    return dot / (left_norm * right_norm)


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
