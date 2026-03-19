from __future__ import annotations

import random

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.user import User
from app.services.config import ConfigService
from app.services.conversation import ConversationService
from app.services.image import ImageService
from app.services.memory import MemoryService
from app.services.message import MessageService
from app.services.prompt import PromptService
from app.services.schedule import ScheduleService


class ProactiveService:
    def __init__(
        self,
        config_service: ConfigService,
        conversation_service: ConversationService,
        prompt_service: PromptService,
        message_service: MessageService,
        schedule_service: ScheduleService,
        image_service: ImageService,
        memory_service: MemoryService,
    ) -> None:
        self.config_service = config_service
        self.conversation_service = conversation_service
        self.prompt_service = prompt_service
        self.message_service = message_service
        self.schedule_service = schedule_service
        self.image_service = image_service
        self.memory_service = memory_service

    async def scan(self, session: AsyncSession) -> int:
        users = (
            await session.execute(select(User).where(User.is_enabled.is_(True)))
        ).scalars().all()
        sent = 0
        for user in users:
            persona = await self.conversation_service.get_active_persona(session, user)
            config = await self.config_service.get_effective_config(
                session,
                user=user,
                persona=persona,
            )
            decision = await self.schedule_service.should_send_proactive_message(
                session,
                user=user,
                persona_id=persona.id if persona else None,
                config=config,
            )
            if not decision.allowed or persona is None:
                continue
            conversation = await self.conversation_service.get_or_create_conversation(
                session,
                user=user,
                persona=persona,
            )
            recent_messages = await self.conversation_service.recent_messages(
                session,
                conversation_id=conversation.id,
                limit=int(config["messaging"]["max_recent_context_messages"]),
            )
            memory_hits = await self.memory_service.retrieve(
                session,
                user_id=user.id,
                persona_id=persona.id,
                query="proactive companion check-in",
                top_k=int(config["memory"]["top_k"]),
                threshold=float(config["memory"]["similarity_threshold"]),
            )
            scene_hint = random.choice(persona.favorite_activities or ["a calm moment at home"])
            context = {
                "user": user,
                "persona": persona,
                "conversation": conversation,
                "recent_messages": recent_messages,
                "memory_hits": memory_hits,
                "scene_hint": scene_hint,
                "config": config,
            }
            instructions = await self.prompt_service.render(session, "system_prompt", context)
            prompt = await self.prompt_service.render(session, "proactive_message", context)
            if self.message_service.openai_provider.enabled:
                response = await self.message_service.openai_provider.generate_text(
                    instructions=instructions,
                    input_items=[{"role": "user", "content": prompt}],
                    max_output_tokens=int(config["openai"]["proactive_max_output_tokens"]),
                    temperature=float(config["openai"]["temperature"]),
                )
                body = response.text
            else:
                body = "Thinking of you and hoping your day is going gently. What have you been up to?"
            media_assets = None
            if random.random() <= float(config["messaging"]["proactive_image_probability"]):
                image_count = await self.schedule_service.image_count_today(
                    session,
                    user_id=user.id,
                    timezone_name=user.timezone,
                )
                if image_count < int(config["safety"]["daily_image_cap"]):
                    asset = await self.image_service.generate_image(
                        session,
                        persona=persona,
                        user=user,
                        scene_hint=scene_hint,
                        config=config,
                        metadata={"source": "proactive"},
                    )
                    if asset.generation_status == "ready":
                        media_assets = [asset]
            await self.message_service.send_outbound_message(
                session,
                user=user,
                persona=persona,
                conversation=conversation,
                body=body,
                is_proactive=True,
                media_assets=media_assets,
            )
            sent += 1
        return sent

    async def trigger_for_user(self, session: AsyncSession, *, user_id) -> int:
        user = await session.get(User, user_id)
        if user is None:
            return 0
        persona = await self.conversation_service.get_active_persona(session, user)
        if persona is None:
            return 0
        conversation = await self.conversation_service.get_or_create_conversation(
            session,
            user=user,
            persona=persona,
        )
        body = f"{persona.display_name} wanted to check in and say hi. How are you doing right now?"
        await self.message_service.send_outbound_message(
            session,
            user=user,
            persona=persona,
            conversation=conversation,
            body=body,
            is_proactive=True,
            ignore_quiet_hours=True,
        )
        return 1
