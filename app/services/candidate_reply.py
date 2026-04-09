from __future__ import annotations

from typing import Any

from sqlalchemy.ext.asyncio import AsyncSession

from app.models.communication import Conversation, Message
from app.models.persona import Persona
from app.models.user import User
from app.providers.openai import OpenAIProvider
from app.services.prompt import PromptService


class CandidateReplyService:
    def __init__(self, openai_provider: OpenAIProvider, prompt_service: PromptService) -> None:
        self.openai_provider = openai_provider
        self.prompt_service = prompt_service

    async def generate_candidates(
        self,
        session: AsyncSession,
        *,
        user: User,
        persona: Persona | None,
        conversation: Conversation,
        inbound_message: Message,
        recent_messages: list[Message],
        memory_hits: list[Any],
        config: dict[str, Any],
        conversation_state: Any,
        classification: dict[str, Any],
    ) -> list[str]:
        context = {
            "user": user,
            "persona": persona,
            "conversation": conversation,
            "inbound_message": inbound_message,
            "recent_messages": recent_messages,
            "memory_hits": memory_hits,
            "config": config,
            "conversation_state": conversation_state,
            "classification": classification,
        }
        instructions = await self.prompt_service.render(session, "system_prompt", context)
        user_prompt = await self.prompt_service.render(session, "reactive_reply", context)

        if not self.openai_provider.enabled:
            return [user_prompt[:240]]

        payload = (
            "Generate 3 distinct SMS reply candidates as JSON only. "
            "Format: {\"candidates\": [\"...\", \"...\", \"...\"]}.\n"
            "Rules:\n"
            "- Candidate A: direct and grounded.\n"
            "- Candidate B: warmer/empathic.\n"
            "- Candidate C: playful but still safe.\n"
            "- If the user asked a direct question, answer it in each candidate before any follow-up.\n"
            "- Keep each candidate concise and under message length limits.\n"
            "- Avoid robotic disclaimers.\n\n"
            f"Planning context:\n{user_prompt}"
        )
        response = await self.openai_provider.generate_json(
            instructions=instructions,
            input_items=[{"role": "user", "content": payload}],
            max_output_tokens=480,
        )
        if isinstance(response, dict) and isinstance(response.get("candidates"), list):
            candidates = [str(item).strip() for item in response["candidates"] if str(item).strip()]
            if candidates:
                return candidates[:3]
        fallback = await self.openai_provider.generate_text(
            instructions=instructions,
            input_items=[{"role": "user", "content": user_prompt}],
            max_output_tokens=int(config["openai"].get("max_output_tokens", 220)),
            temperature=float(config["openai"].get("temperature", 0.8)),
        )
        text = fallback.text.strip()
        return [text] if text else []
