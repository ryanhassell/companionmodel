from __future__ import annotations

import math
from dataclasses import dataclass
from typing import Any

from sqlalchemy import desc, select, text
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.settings import RuntimeSettings
from app.models.communication import Message
from app.models.enums import Direction, MemoryType
from app.models.memory import MemoryItem
from app.models.persona import Persona
from app.models.user import User
from app.providers.openai import OpenAIProvider
from app.services.prompt import PromptService
from app.utils.time import utc_now


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
            memory = MemoryItem(
                user_id=user.id,
                persona_id=persona.id if persona else None,
                source_message_id=message.id,
                memory_type=MemoryType(item.get("memory_type", "fact")),
                title=item.get("title"),
                content=content,
                summary=item.get("summary"),
                tags=item.get("tags", []),
                importance_score=float(item.get("importance_score", 0.5)),
                metadata_json={"source": "extraction"},
            )
            session.add(memory)
            created.append(memory)
        await session.flush()
        if created:
            await self.embed_items(session, created, config=config)
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
        sql = text(
            """
            SELECT id, 1 - (embedding_vector <=> CAST(:vector_literal AS vector)) AS similarity
            FROM memory_items
            WHERE user_id = :user_id
              AND disabled = false
              AND embedding_vector IS NOT NULL
              AND (:persona_id IS NULL OR persona_id IS NULL OR persona_id = :persona_id)
            ORDER BY embedding_vector <=> CAST(:vector_literal AS vector)
            LIMIT :top_k
            """
        )
        rows = (await session.execute(sql, {"vector_literal": vector_literal, "user_id": user_id, "persona_id": persona_id, "top_k": top_k})).all()
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
        return f"type={item.memory_type.value}\ntitle={item.title or ''}\ntags={tags}\ncontent={item.content}"

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


def cosine_similarity(left: list[float], right: list[float]) -> float:
    dot = sum(a * b for a, b in zip(left, right))
    left_norm = math.sqrt(sum(a * a for a in left))
    right_norm = math.sqrt(sum(b * b for b in right))
    if left_norm == 0 or right_norm == 0:
        return 0.0
    return dot / (left_norm * right_norm)
