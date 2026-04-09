from __future__ import annotations

from typing import Any

from sqlalchemy.ext.asyncio import AsyncSession

from app.models.communication import Conversation, Message
from app.models.persona import Persona
from app.models.user import User
from app.providers.openai import OpenAIProvider
from app.services.prompt import PromptService
from app.utils.text import normalize_text


class TurnClassifierService:
    def __init__(self, openai_provider: OpenAIProvider, prompt_service: PromptService) -> None:
        self.openai_provider = openai_provider
        self.prompt_service = prompt_service

    async def classify(
        self,
        session: AsyncSession,
        *,
        user: User,
        persona: Persona | None,
        conversation: Conversation,
        inbound_message: Message,
        recent_messages: list[Message],
        config: dict[str, Any],
        conversation_state: Any,
    ) -> dict[str, Any]:
        text = inbound_message.body or ""
        fallback = self._heuristic(text)
        if not self.openai_provider.enabled:
            return fallback

        context = {
            "user": user,
            "persona": persona,
            "conversation": conversation,
            "inbound_message": inbound_message,
            "recent_messages": recent_messages,
            "memory_hits": [],
            "config": config,
            "conversation_state": conversation_state,
        }
        instructions = await self.prompt_service.render(session, "system_prompt", context)
        payload = (
            "Classify the user's latest message for response planning. Return JSON only with keys: "
            "intent, emotion, direct_question, needs_reassurance, risk_flags, response_energy.\n"
            "intent should be one of: question, update, request, emotional_share, banter, mixed.\n"
            "emotion should be one of: neutral, happy, playful, sad, anxious, distressed, frustrated.\n"
            "direct_question and needs_reassurance are booleans. risk_flags is a list of short strings.\n"
            "response_energy should be: low, medium, high.\n"
            f"Message: {text}\n"
        )
        response = await self.openai_provider.generate_json(
            instructions=instructions,
            input_items=[{"role": "user", "content": payload}],
            max_output_tokens=220,
        )
        if not isinstance(response, dict):
            return fallback
        merged = {**fallback, **response}
        merged["risk_flags"] = [str(item).strip() for item in (merged.get("risk_flags") or []) if str(item).strip()][:6]
        merged["direct_question"] = bool(merged.get("direct_question"))
        merged["needs_reassurance"] = bool(merged.get("needs_reassurance"))
        return merged

    def _heuristic(self, text: str) -> dict[str, Any]:
        normalized = normalize_text(text)
        return {
            "intent": "question" if "?" in text else "update",
            "emotion": "distressed" if any(token in normalized for token in ["hate myself", "panic", "worthless"]) else "neutral",
            "direct_question": "?" in text,
            "needs_reassurance": any(token in normalized for token in ["scared", "alone", "bad day", "anxious"]),
            "risk_flags": ["dependency_language"] if any(token in normalized for token in ["only you", "need you"]) else [],
            "response_energy": "low" if len(text.split()) < 5 else "medium",
        }
