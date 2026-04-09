from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.communication import Conversation, Message
from app.models.conversation_state import ConversationState
from app.models.enums import Direction
from app.models.persona import Persona
from app.models.user import User
from app.utils.time import utc_now


@dataclass(slots=True)
class ConversationStateContext:
    state: ConversationState
    active_topic: str | None
    unresolved_loop: str | None


class ConversationStateService:
    async def get_or_create(
        self,
        session: AsyncSession,
        *,
        user: User,
        persona: Persona | None,
        conversation: Conversation,
    ) -> ConversationState:
        stmt = select(ConversationState).where(ConversationState.conversation_id == conversation.id)
        state = (await session.execute(stmt)).scalar_one_or_none()
        if state is not None:
            if persona and state.persona_id != persona.id:
                state.persona_id = persona.id
            return state
        state = ConversationState(
            conversation_id=conversation.id,
            user_id=user.id,
            persona_id=persona.id if persona else None,
            style_fingerprint={
                "emoji_tolerance": "low",
                "slang_tolerance": "medium",
                "max_sentences": 3,
                "question_cadence": "balanced",
            },
        )
        session.add(state)
        await session.flush()
        return state

    async def update_from_inbound(
        self,
        session: AsyncSession,
        *,
        state: ConversationState,
        inbound_text: str,
        classification: dict[str, Any],
        recent_messages: list[Message],
    ) -> ConversationState:
        text = (inbound_text or "").strip()
        if not text:
            return state
        lowered = text.lower()
        topics = list(state.active_topics or [])
        inferred_topics = [item for item in _infer_topics(text) if item not in topics]
        state.active_topics = (inferred_topics + topics)[:8]

        questions = list(state.last_user_questions or [])
        if "?" in text:
            questions.insert(0, text[:220])
        state.last_user_questions = questions[:8]

        if bool(classification.get("direct_question")) and text:
            open_loops = list(state.open_loops or [])
            open_loops.insert(
                0,
                {
                    "kind": "user_question",
                    "text": text[:220],
                    "created_at": utc_now().isoformat(),
                    "status": "open",
                },
            )
            state.open_loops = open_loops[:10]

        sentiment = str(classification.get("emotion") or "neutral").strip().lower()
        if sentiment in {"distressed", "sad", "overwhelmed", "anxious"}:
            state.recent_mood_trend = "down"
        elif sentiment in {"happy", "excited", "playful", "calm"}:
            state.recent_mood_trend = "up"
        else:
            state.recent_mood_trend = sentiment or state.recent_mood_trend

        pressure = float(state.boundary_pressure_score or 0.0)
        if any(token in lowered for token in ["dont leave", "don't leave", "need you", "only you"]):
            pressure += 0.25
        else:
            pressure = max(0.0, pressure - 0.05)
        state.boundary_pressure_score = min(1.0, pressure)

        style = dict(state.style_fingerprint or {})
        style.update(_style_update_from_recent(recent_messages))
        state.style_fingerprint = style

        state.continuity_card = _build_continuity_card(state)
        await session.flush()
        return state

    async def update_from_outbound(
        self,
        session: AsyncSession,
        *,
        state: ConversationState,
        outbound_text: str,
        is_proactive: bool,
    ) -> ConversationState:
        text = (outbound_text or "").strip().lower()
        if not text:
            return state
        open_loops = []
        for loop in state.open_loops or []:
            if loop.get("status") == "closed":
                continue
            loop_text = str(loop.get("text") or "").lower()
            if loop_text and _roughly_addressed(loop_text, text):
                loop["status"] = "closed"
                loop["closed_at"] = utc_now().isoformat()
            else:
                open_loops.append(loop)
        state.open_loops = open_loops[:10]

        novelty = float(state.novelty_budget or 1.0)
        novelty -= 0.2 if not is_proactive else 0.1
        state.novelty_budget = max(0.15, novelty)

        fatigue = float(state.fatigue_score or 0.0)
        if is_proactive:
            fatigue = min(1.0, fatigue + 0.08)
        else:
            fatigue = max(0.0, fatigue - 0.06)
        state.fatigue_score = fatigue
        state.continuity_card = _build_continuity_card(state)
        await session.flush()
        return state

    async def mark_proactive_archetype(
        self,
        session: AsyncSession,
        *,
        state: ConversationState,
        archetype: str,
    ) -> None:
        state.last_archetype = archetype
        await session.flush()

    def context(self, state: ConversationState) -> ConversationStateContext:
        active_topic = (state.active_topics or [None])[0]
        unresolved_loop = None
        for item in state.open_loops or []:
            if item.get("status") == "open":
                unresolved_loop = str(item.get("text") or "").strip() or None
                if unresolved_loop:
                    break
        return ConversationStateContext(state=state, active_topic=active_topic, unresolved_loop=unresolved_loop)


STOPWORDS = {
    "the",
    "and",
    "with",
    "that",
    "this",
    "have",
    "just",
    "about",
    "your",
    "what",
    "when",
    "where",
    "would",
}


def _infer_topics(text: str) -> list[str]:
    words = [part.strip(".,!?;:()[]{}\"'").lower() for part in text.split()]
    tokens = [word for word in words if len(word) >= 4 and word not in STOPWORDS]
    seen: list[str] = []
    for token in tokens:
        if token not in seen:
            seen.append(token)
    return seen[:3]


def _style_update_from_recent(messages: list[Message]) -> dict[str, Any]:
    outbound = [m for m in messages[-8:] if m.direction == Direction.outbound and m.body]
    if not outbound:
        return {}
    joined = "\n".join((m.body or "") for m in outbound)
    question_count = joined.count("?")
    emoji_count = sum(1 for ch in joined if ord(ch) > 10000)
    sentence_like = max(joined.count("."), 1)
    return {
        "question_cadence": "high" if question_count >= 4 else "balanced",
        "emoji_tolerance": "medium" if emoji_count >= 2 else "low",
        "max_sentences": 2 if sentence_like < 4 else 3,
    }


def _roughly_addressed(question_text: str, answer_text: str) -> bool:
    for token in question_text.split():
        clean = token.strip(".,!?;:").lower()
        if len(clean) < 5:
            continue
        if clean in answer_text:
            return True
    return "yes" in answer_text or "no" in answer_text


def _build_continuity_card(state: ConversationState) -> str:
    parts: list[str] = []
    if state.active_topics:
        parts.append(f"Top topic: {state.active_topics[0]}")
    for loop in state.open_loops or []:
        if loop.get("status") == "open" and loop.get("text"):
            parts.append(f"Open loop: {str(loop['text'])[:120]}")
            break
    if state.recent_mood_trend:
        parts.append(f"Mood trend: {state.recent_mood_trend}")
    return " | ".join(parts)[:280]
