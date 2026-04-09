from __future__ import annotations

from typing import Any

from sqlalchemy.ext.asyncio import AsyncSession

from app.ai import AiRuntime
from app.models.communication import Conversation
from app.models.persona import Persona
from app.models.user import User
from app.services.prompt import PromptService
from app.utils.text import truncate_text


class SafetyRewriteService:
    def __init__(self, ai_runtime: AiRuntime, prompt_service: PromptService) -> None:
        self.ai_runtime = ai_runtime
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
            return ""
        if not self.ai_runtime.enabled:
            return ""

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
        try:
            response = await self.ai_runtime.rewrite_safely(
                instructions=instructions,
                prompt=(
                    f"{template}\n\n"
                    f"Reasons to fix: {', '.join(reasons) if reasons else 'unknown'}\n"
                    "Return one safe rewrite only, casual and warm, under 220 characters."
                ),
                max_tokens=120,
                temperature=0.6,
            )
        except Exception:
            return ""
        rewritten = (response.output.text or "").strip()
        return truncate_text(rewritten, int(config["messaging"].get("max_message_length", 480)))
