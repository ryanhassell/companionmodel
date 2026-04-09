from __future__ import annotations

from typing import Any

from sqlalchemy.ext.asyncio import AsyncSession

from app.models.communication import Conversation
from app.models.persona import Persona
from app.models.user import User
from app.providers.openai import OpenAIProvider
from app.services.prompt import PromptService
from app.utils.text import truncate_text


class SafetyRewriteService:
    def __init__(self, openai_provider: OpenAIProvider, prompt_service: PromptService) -> None:
        self.openai_provider = openai_provider
        self.prompt_service = prompt_service

    async def rewrite(
        self,
        session: AsyncSession,
        *,
        original_text: str,
        reasons: list[str],
        user: User,
        persona: Persona | None,
        conversation: Conversation,
        config: dict[str, Any],
        context: dict[str, Any],
    ) -> str:
        if not original_text.strip():
            return self._fallback_variant(reasons)
        if not self.openai_provider.enabled:
            return self._fallback_variant(reasons)

        template = await self.prompt_service.render(
            session,
            "safety_rewrite",
            {
                "text": original_text,
                "reasons": reasons,
                "user": user,
                "persona": persona,
                "conversation": conversation,
            },
        )
        instructions = await self.prompt_service.render(session, "system_prompt", context)
        response = await self.openai_provider.generate_text(
            instructions=instructions,
            input_items=[
                {
                    "role": "user",
                    "content": (
                        f"{template}\n\n"
                        f"Reasons to fix: {', '.join(reasons) if reasons else 'unknown'}\n"
                        "Return one safe rewrite only, casual and warm, under 220 characters."
                    ),
                }
            ],
            max_output_tokens=120,
            temperature=0.6,
        )
        rewritten = (response.text or "").strip()
        if not rewritten:
            rewritten = self._fallback_variant(reasons)
        return truncate_text(rewritten, int(config["messaging"].get("max_message_length", 480)))

    def _fallback_variant(self, reasons: list[str]) -> str:
        bank = [
            "I care about you and want to keep this safe and steady. We can keep talking in a grounded way.",
            "I'm here with you, and I want to phrase this in a safer way so our chats stay supportive.",
            "I want this to feel warm and safe for both of us, so I'm going to say that a little differently.",
        ]
        if not reasons:
            return bank[0]
        index = sum(ord(char) for char in "|".join(reasons)) % len(bank)
        return bank[index]
