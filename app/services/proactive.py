from __future__ import annotations

import random

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.user import User
from app.utils.time import utc_now
from app.services.voice import VoiceService
from app.services.config import ConfigService
from app.services.conversation import ConversationService
from app.services.conversation_state import ConversationStateService
from app.services.daily_life import DailyLifeService
from app.services.image import ImageService
from app.services.memory import MemoryService
from app.services.message import MessageService
from app.services.prompt import PromptService
from app.services.schedule import ScheduleService
from app.services.usage_ingestion import UsageIngestionService


class ProactiveService:
    def __init__(
        self,
        config_service: ConfigService,
        conversation_service: ConversationService,
        prompt_service: PromptService,
        message_service: MessageService,
        schedule_service: ScheduleService,
        daily_life_service: DailyLifeService,
        image_service: ImageService,
        memory_service: MemoryService,
        voice_service: VoiceService,
        conversation_state_service: ConversationStateService,
        usage_ingestion_service: UsageIngestionService | None = None,
    ) -> None:
        self.config_service = config_service
        self.conversation_service = conversation_service
        self.prompt_service = prompt_service
        self.message_service = message_service
        self.schedule_service = schedule_service
        self.daily_life_service = daily_life_service
        self.image_service = image_service
        self.memory_service = memory_service
        self.voice_service = voice_service
        self.conversation_state_service = conversation_state_service
        self.usage_ingestion_service = usage_ingestion_service

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
            if persona is not None and self._persona_can_call_user(persona, user.phone_number):
                call_decision = await self.schedule_service.should_send_proactive_call(
                    session,
                    user=user,
                    persona_id=persona.id,
                    config=config,
                )
                if call_decision.allowed:
                    opening_line = await self._generate_proactive_call_opening(
                        session,
                        user=user,
                        persona=persona,
                        config=config,
                    )
                    await self.voice_service.initiate_call(
                        session,
                        user=user,
                        persona=persona,
                        config=config,
                        opening_line=opening_line,
                    )
                    user.last_outbound_at = utc_now()
                    sent += 1
                    continue
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
            state = await self.conversation_state_service.get_or_create(
                session,
                user=user,
                persona=persona,
                conversation=conversation,
            )
            fatigue_threshold = float(config.get("human_likeness", {}).get("proactive_fatigue_threshold", 0.82))
            if float(state.fatigue_score or 0.0) > fatigue_threshold:
                continue
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
            daily_context = await self.daily_life_service.prompt_context(
                session,
                user=user,
                persona=persona,
                config=config,
            )
            scene_hint = str(
                daily_context.get("proactive_photo_scene_hint")
                or random.choice(persona.favorite_activities or ["a calm moment at home"])
            )
            archetype = _next_archetype(state.last_archetype)
            context = {
                "user": user,
                "persona": persona,
                "conversation": conversation,
                "recent_messages": recent_messages,
                "memory_hits": memory_hits,
                "scene_hint": scene_hint,
                "proactive_archetype": archetype,
                "conversation_state": state,
                "config": config,
                **daily_context,
            }
            instructions = await self.prompt_service.render(session, "system_prompt", context)
            prompt = await self.prompt_service.render(session, "proactive_message", context)
            body = ""
            if self.message_service.ai_runtime.enabled:
                try:
                    response = await self.message_service.ai_runtime.proactive_message(
                        instructions=instructions,
                        prompt=prompt,
                        max_tokens=int(config["openai"]["proactive_max_output_tokens"]),
                        temperature=float(config["openai"]["temperature"]),
                    )
                    body = response.output.text
                except Exception:
                    body = ""
            if not body.strip():
                continue
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
                        use_reference_image=bool(daily_context.get("proactive_photo_include_person")),
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
            await self.conversation_state_service.mark_proactive_archetype(
                session,
                state=state,
                archetype=archetype,
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

    async def _generate_proactive_call_opening(
        self,
        session: AsyncSession,
        *,
        user: User,
        persona,
        config: dict,
    ) -> str:
        conversation = await self.conversation_service.get_or_create_conversation(
            session,
            user=user,
            persona=persona,
        )
        recent_messages = await self.conversation_service.recent_messages(
            session,
            conversation_id=conversation.id,
            limit=min(int(config["messaging"]["max_recent_context_messages"]), 8),
        )
        memory_hits = await self.memory_service.retrieve(
            session,
            user_id=user.id,
            persona_id=persona.id if persona else None,
            query="proactive voice call opener, recent context, current day details",
            top_k=5,
            threshold=float(config["memory"]["similarity_threshold"]),
        )
        daily_context = await self.daily_life_service.prompt_context(
            session,
            user=user,
            persona=persona,
            config=config,
        )
        context = {
            "user": user,
            "persona": persona,
            "conversation": conversation,
            "recent_messages": recent_messages,
            "memory_hits": memory_hits,
            "conversation_state": await self.conversation_state_service.get_or_create(
                session,
                user=user,
                persona=persona,
                conversation=conversation,
            ),
            "config": config,
            **daily_context,
        }
        instructions = await self.prompt_service.render(session, "system_prompt", context)
        prompt = (
            "Write one short in-character phone call opener.\n"
            "It should sound like a normal casual person calling, not an assistant.\n"
            "Carry the conversation yourself with a tiny update, thought, or reason for calling.\n"
            "Do not ask more than one short question.\n"
            "Keep it under 140 characters."
        )
        if self.message_service.ai_runtime.enabled:
            try:
                response = await self.message_service.ai_runtime.proactive_call_opening(
                    instructions=instructions,
                    prompt=prompt,
                    max_tokens=70,
                )
                return response.output.text.strip()
            except Exception:
                return ""
        return ""

    def _persona_can_call_user(self, persona, phone_number: str) -> bool:
        prompt_overrides = getattr(persona, "prompt_overrides", {}) or {}
        raw_numbers = prompt_overrides.get("calling_numbers") or []
        if not isinstance(raw_numbers, list):
            return False
        normalized_target = _normalize_phone_number(phone_number)
        return any(_normalize_phone_number(str(number)) == normalized_target for number in raw_numbers if str(number).strip())


def _normalize_phone_number(value: str) -> str:
    raw = (value or "").strip()
    if not raw:
        return ""
    has_plus = raw.startswith("+")
    digits = "".join(ch for ch in raw if ch.isdigit())
    if not digits:
        return raw.lower()
    if has_plus:
        return f"+{digits}"
    if len(digits) == 11 and digits.startswith("1"):
        return f"+{digits}"
    if len(digits) == 10:
        return f"+1{digits}"
    return digits


def _next_archetype(last_archetype: str | None) -> str:
    order = ["check_in", "tiny_update", "memory_callback", "light_prompt"]
    if not last_archetype or last_archetype not in order:
        return order[0]
    index = order.index(last_archetype)
    return order[(index + 1) % len(order)]
