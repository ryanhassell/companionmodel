from __future__ import annotations

import asyncio
import random
from datetime import timedelta
from typing import Any

from sqlalchemy import desc, func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.logging import get_logger
from app.core.settings import RuntimeSettings
from app.db.session import get_sessionmaker
from app.models.communication import Conversation, DeliveryAttempt, MediaAsset, Message
from app.models.enums import Channel, DeliveryStatus, Direction, MediaRole, MessageStatus
from app.models.persona import Persona
from app.models.user import User
from app.providers.base import InboundMessagePayload
from app.providers.openai import OpenAIProvider
from app.providers.twilio import TwilioProvider
from app.services.config import ConfigService
from app.services.conversation import ConversationService
from app.services.daily_life import DailyLifeService
from app.services.image import ImageService
from app.services.memory import MemoryService
from app.services.prompt import PromptService
from app.services.safety import SafetyService
from app.services.schedule import ScheduleService
from app.utils.text import make_idempotency_key, normalize_text, similarity_score, truncate_text
from app.utils.time import utc_now

logger = get_logger(__name__)


class MessageService:
    def __init__(
        self,
        settings: RuntimeSettings,
        twilio_provider: TwilioProvider,
        openai_provider: OpenAIProvider,
        prompt_service: PromptService,
        safety_service: SafetyService,
        memory_service: MemoryService,
        conversation_service: ConversationService,
        daily_life_service: DailyLifeService,
        schedule_service: ScheduleService,
        config_service: ConfigService,
        image_service: ImageService,
    ) -> None:
        self.settings = settings
        self.twilio_provider = twilio_provider
        self.openai_provider = openai_provider
        self.prompt_service = prompt_service
        self.safety_service = safety_service
        self.memory_service = memory_service
        self.conversation_service = conversation_service
        self.daily_life_service = daily_life_service
        self.schedule_service = schedule_service
        self.config_service = config_service
        self.image_service = image_service

    async def handle_inbound_message(self, session: AsyncSession, payload: InboundMessagePayload) -> Message:
        existing = await session.scalar(select(Message).where(Message.provider_message_sid == payload.message_sid))
        if existing:
            return existing

        user = await self.conversation_service.get_or_create_user_by_phone(session, payload.from_number)
        persona = await self.conversation_service.get_active_persona(session, user)
        config = await self.config_service.get_effective_config(session, user=user, persona=persona)
        conversation = await self.conversation_service.get_or_create_conversation(session, user=user, persona=persona)

        inbound = Message(
            conversation_id=conversation.id,
            user_id=user.id,
            persona_id=persona.id if persona else None,
            direction=Direction.inbound,
            channel=Channel.mms if payload.num_media else Channel.sms,
            provider="twilio",
            provider_message_sid=payload.message_sid,
            idempotency_key=make_idempotency_key(payload.message_sid),
            body=payload.body,
            normalized_body=normalize_text(payload.body),
            status=MessageStatus.received,
            metadata_json=payload.raw_form,
        )
        session.add(inbound)
        await session.flush()
        self.conversation_service.mark_inbound(user, conversation)
        for media in payload.media:
            session.add(
                MediaAsset(
                    message_id=inbound.id,
                    user_id=user.id,
                    persona_id=persona.id if persona else None,
                    role=MediaRole.inbound,
                    remote_url=media.url,
                    mime_type=media.content_type,
                    metadata_json={"source": "twilio"},
                )
            )
        await session.flush()

        recent_window_stmt = select(func.count()).select_from(Message).where(
            Message.user_id == user.id,
            Message.direction == Direction.inbound,
            Message.created_at >= utc_now() - timedelta(minutes=int(config["safety"]["obsessive_window_minutes"])),
        )
        recent_inbound_count = int((await session.scalar(recent_window_stmt)) or 0)
        safety = await self.safety_service.evaluate_inbound(
            session,
            text=payload.body or "",
            user=user,
            persona=persona,
            conversation=conversation,
            message=inbound,
            config=config,
            recent_inbound_count=recent_inbound_count,
        )

        recent_messages = await self.conversation_service.recent_messages(
            session,
            conversation_id=conversation.id,
            limit=int(config["messaging"]["max_recent_context_messages"]),
        )
        action = await self._decide_inbound_action(
            session,
            user=user,
            persona=persona,
            conversation=conversation,
            inbound_text=payload.body or "",
            recent_messages=recent_messages,
            config=config,
        )
        if safety.safe_reply:
            reply_text = safety.safe_reply
            media_assets = None
        elif action["send_image"]:
            reply_text = action["reply_text"] or await self._generate_photo_status_reply(
                session,
                user=user,
                persona=persona,
                conversation=conversation,
                inbound_text=payload.body or "",
                config=config,
                mode="ack",
            )
            media_assets = None
        else:
            media_assets = None
            reply_text = await self.generate_reply(
                session,
                user=user,
                persona=persona,
                conversation=conversation,
                inbound_message=inbound,
                recent_messages=recent_messages,
                config=config,
            )
        outbound = await self.send_outbound_message(
            session,
            user=user,
            persona=persona,
            conversation=conversation,
            body=reply_text,
            is_proactive=False,
            media_assets=media_assets,
        )
        if action["send_image"] and not safety.safe_reply:
            self._enqueue_explicit_photo_reply(
                user_id=user.id,
                persona_id=persona.id if persona else None,
                conversation_id=conversation.id,
                inbound_text=payload.body or "",
                provider="twilio",
                scene_hint=action.get("scene_hint"),
                include_person=action.get("include_person"),
            )
        if inbound.body:
            await self.memory_service.extract_from_message(
                session,
                user=user,
                persona=persona,
                message=inbound,
                recent_messages=recent_messages,
                config=config,
            )
        await session.flush()
        return outbound

    async def simulate_inbound_message(
        self,
        session: AsyncSession,
        *,
        user: User,
        body: str,
        persona: Persona | None = None,
        force_photo: bool = False,
    ) -> tuple[Message, Message]:
        persona = persona or await self.conversation_service.get_active_persona(session, user)
        config = await self.config_service.get_effective_config(session, user=user, persona=persona)
        conversation = await self.conversation_service.get_or_create_conversation(session, user=user, persona=persona)

        inbound = Message(
            conversation_id=conversation.id,
            user_id=user.id,
            persona_id=persona.id if persona else None,
            direction=Direction.inbound,
            channel=Channel.sms,
            provider="simulator",
            provider_message_sid=None,
            idempotency_key=make_idempotency_key("simulator-inbound", user.id, body, utc_now()),
            body=body,
            normalized_body=normalize_text(body),
            status=MessageStatus.received,
            metadata_json={"source": "admin_simulator"},
        )
        session.add(inbound)
        await session.flush()
        self.conversation_service.mark_inbound(user, conversation)

        safety = await self.safety_service.evaluate_inbound(
            session,
            text=body,
            user=user,
            persona=persona,
            conversation=conversation,
            message=inbound,
            config=config,
            recent_inbound_count=0,
        )

        recent_messages = await self.conversation_service.recent_messages(
            session,
            conversation_id=conversation.id,
            limit=int(config["messaging"]["max_recent_context_messages"]),
        )
        action = await self._decide_inbound_action(
            session,
            user=user,
            persona=persona,
            conversation=conversation,
            inbound_text=body,
            recent_messages=recent_messages,
            config=config,
            force_photo=force_photo,
        )
        if safety.safe_reply:
            reply_text = safety.safe_reply
            media_assets = None
        elif action["send_image"]:
            media_assets = await self._generate_explicit_photo_with_retries(
                session,
                user=user,
                persona=persona,
                conversation=conversation,
                inbound_text=body,
                config=config,
                provider="simulator",
                scene_hint=action.get("scene_hint"),
                include_person=action.get("include_person"),
            )
            reply_text = await self._generate_photo_status_reply(
                session,
                user=user,
                persona=persona,
                conversation=conversation,
                inbound_text=body,
                config=config,
                mode="success" if media_assets else "failure",
            )
        else:
            media_assets = None
            reply_text = await self.generate_reply(
                session,
                user=user,
                persona=persona,
                conversation=conversation,
                inbound_message=inbound,
                recent_messages=recent_messages,
                config=config,
            )
        outbound = Message(
            conversation_id=conversation.id,
            user_id=user.id,
            persona_id=persona.id if persona else None,
            direction=Direction.outbound,
            channel=Channel.mms if media_assets else Channel.sms,
            provider="simulator",
            provider_message_sid=None,
            idempotency_key=make_idempotency_key("simulator-outbound", conversation.id, reply_text, utc_now()),
            body=reply_text,
            normalized_body=normalize_text(reply_text),
            status=MessageStatus.sent,
            is_proactive=False,
            sent_at=utc_now(),
            metadata_json={"source": "admin_simulator"},
        )
        session.add(outbound)
        await session.flush()
        for asset in media_assets or []:
            asset.message_id = outbound.id
        self.conversation_service.mark_outbound(user, conversation)

        await self.memory_service.extract_from_message(
            session,
            user=user,
            persona=persona,
            message=inbound,
            recent_messages=recent_messages,
            config=config,
        )
        await session.flush()
        return inbound, outbound

    async def _decide_inbound_action(
        self,
        session: AsyncSession,
        *,
        user: User,
        persona: Persona | None,
        conversation: Conversation,
        inbound_text: str,
        recent_messages: list[Message],
        config: dict[str, Any],
        force_photo: bool = False,
    ) -> dict[str, Any]:
        if persona is None or not self.openai_provider.enabled:
            return {
                "send_image": force_photo,
                "reply_text": "",
                "scene_hint": None,
                "include_person": None,
                "reason": "model_unavailable",
            }
        image_count = await self.schedule_service.image_count_today(
            session,
            user_id=user.id,
            timezone_name=user.timezone,
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
            "inbound_message": Message(body=inbound_text),
            "recent_messages": recent_messages,
            "memory_hits": [],
            "config": config,
            **daily_context,
        }
        instructions = await self.prompt_service.render(session, "system_prompt", context)
        recent_lines = []
        for message in recent_messages[-8:]:
            role = "user" if message.direction == Direction.inbound else "companion"
            recent_lines.append(f"- {role}: {message.body or ''}")
        try:
            response = await self.openai_provider.generate_json(
                instructions=instructions,
                input_items=[
                    {
                        "role": "user",
                        "content": (
                            "Decide the next messaging action for this incoming SMS.\n"
                            "Return JSON only in this format: "
                            "{\"send_image\": true/false, \"reply_text\": \"...\", "
                            "\"scene_hint\": \"...\", \"include_person\": true/false, \"reason\": \"...\"}.\n\n"
                            "Rules:\n"
                            "- Prefer send_image=true when the user is asking to see something, asking for a selfie/photo/picture, "
                            "or when a picture would be a very natural reply.\n"
                            "- Keep send_image rare unless the user is clearly asking to see something.\n"
                            "- If send_image=true, reply_text must be a very short in-character acknowledgment that you're grabbing or sending it now.\n"
                            "- If send_image=false, set reply_text to an empty string.\n"
                            "- For sunsets, windows, weather, scenery, food, and objects, prefer include_person=false unless the user specifically wants to see you.\n"
                            "- For selfies, outfit requests, or requests to see you, set include_person=true.\n"
                            "- scene_hint should be a short, concrete image brief optimized for generation.\n"
                            "- Do not dodge a valid image request in prose. If a picture should happen, choose send_image=true.\n"
                            f"- Ready images today: {image_count} / {int(config['safety']['daily_image_cap'])}.\n"
                            f"- Force photo mode: {'true' if force_photo else 'false'}. If true, send_image must be true.\n\n"
                            f"Incoming message: {inbound_text}\n\n"
                            "Recent conversation:\n"
                            f"{chr(10).join(recent_lines) if recent_lines else '- none'}"
                        ),
                    }
                ],
                max_output_tokens=220,
            )
            if isinstance(response, dict):
                decision = {
                    "send_image": bool(response.get("send_image")) or force_photo,
                    "reply_text": str(response.get("reply_text") or "").strip(),
                    "scene_hint": str(response.get("scene_hint") or "").strip() or self._reactive_image_scene_hint(inbound_text, persona),
                    "include_person": response.get("include_person"),
                    "reason": str(response.get("reason") or "").strip(),
                }
                logger.info(
                    "inbound_action_decision",
                    user_id=str(user.id),
                    conversation_id=str(conversation.id),
                    persona_id=str(persona.id),
                    inbound_text_preview=truncate_text(inbound_text, 120),
                    send_image=decision["send_image"],
                    include_person=decision["include_person"],
                    scene_hint_preview=truncate_text(decision["scene_hint"], 120),
                    reason_preview=truncate_text(decision["reason"], 120),
                    force_photo=force_photo,
                )
                return decision
        except Exception as exc:
            logger.info(
                "inbound_action_decision_failed",
                inbound_text_preview=truncate_text(inbound_text, 120),
                error=str(exc),
                force_photo=force_photo,
            )
        return {
            "send_image": force_photo,
            "reply_text": "",
            "scene_hint": self._reactive_image_scene_hint(inbound_text, persona),
            "include_person": None,
            "reason": "fallback",
        }

    def _enqueue_explicit_photo_reply(
        self,
        *,
        user_id,
        persona_id,
        conversation_id,
        inbound_text: str,
        provider: str,
        scene_hint: str | None,
        include_person: bool | None,
    ) -> None:
        asyncio.create_task(
            self._complete_explicit_photo_reply(
                user_id=user_id,
                persona_id=persona_id,
                conversation_id=conversation_id,
                inbound_text=inbound_text,
                provider=provider,
                scene_hint=scene_hint,
                include_person=include_person,
            )
        )

    async def _complete_explicit_photo_reply(
        self,
        *,
        user_id,
        persona_id,
        conversation_id,
        inbound_text: str,
        provider: str,
        scene_hint: str | None,
        include_person: bool | None,
    ) -> None:
        sessionmaker = get_sessionmaker()
        try:
            async with sessionmaker() as session:
                user = await session.get(User, user_id)
                conversation = await session.get(Conversation, conversation_id)
                persona = await session.get(Persona, persona_id) if persona_id else None
                if user is None or conversation is None:
                    return
                config = await self.config_service.get_effective_config(session, user=user, persona=persona)
                media_assets = await self._generate_explicit_photo_with_retries(
                    session,
                    user=user,
                    persona=persona,
                    conversation=conversation,
                    inbound_text=inbound_text,
                    config=config,
                    provider=provider,
                    scene_hint=scene_hint,
                    include_person=include_person,
                )
                reply_text = await self._generate_photo_status_reply(
                    session,
                    user=user,
                    persona=persona,
                    conversation=conversation,
                    inbound_text=inbound_text,
                    config=config,
                    mode="success" if media_assets else "failure",
                )
                if provider == "simulator":
                    message = Message(
                        conversation_id=conversation.id,
                        user_id=user.id,
                        persona_id=persona.id if persona else None,
                        direction=Direction.outbound,
                        channel=Channel.mms if media_assets else Channel.sms,
                        provider="simulator",
                        provider_message_sid=None,
                        idempotency_key=make_idempotency_key("simulator-photo-followup", conversation.id, inbound_text, utc_now()),
                        body=reply_text,
                        normalized_body=normalize_text(reply_text),
                        status=MessageStatus.sent,
                        is_proactive=False,
                        sent_at=utc_now(),
                        metadata_json={"source": "admin_simulator", "photo_followup": True},
                    )
                    session.add(message)
                    await session.flush()
                    for asset in media_assets or []:
                        asset.message_id = message.id
                    self.conversation_service.mark_outbound(user, conversation)
                else:
                    await self.send_outbound_message(
                        session,
                        user=user,
                        persona=persona,
                        conversation=conversation,
                        body=reply_text,
                        is_proactive=False,
                        media_assets=media_assets,
                        skip_schedule_check=True,
                    )
                await session.commit()
        except Exception as exc:
            logger.info(
                "photo_followup_failed",
                user_id=str(user_id),
                persona_id=str(persona_id) if persona_id else None,
                provider=provider,
                error=str(exc),
            )

    async def _generate_explicit_photo_with_retries(
        self,
        session: AsyncSession,
        *,
        user: User,
        persona: Persona | None,
        conversation: Conversation,
        inbound_text: str,
        config: dict[str, Any],
        provider: str,
        scene_hint: str | None,
        include_person: bool | None,
    ) -> list[MediaAsset] | None:
        if persona is None:
            return None
        attempt_specs = self._photo_attempt_specs(
            inbound_text,
            persona,
            scene_hint=scene_hint,
            include_person=include_person,
        )
        first_error: str | None = None
        for idx, spec in enumerate(attempt_specs, start=1):
            logger.info(
                "photo_retry_attempt",
                attempt=idx,
                provider=provider,
                persona=persona.display_name,
                inbound_text_preview=truncate_text(inbound_text, 100),
                scene_hint_preview=truncate_text(spec["scene_hint"], 100),
                use_reference_image=bool(spec["use_reference_image"]),
            )
            asset = await self.image_service.generate_image(
                session,
                persona=persona,
                user=user,
                scene_hint=spec["scene_hint"],
                config=config,
                use_reference_image=bool(spec["use_reference_image"]),
                metadata={"source": "reactive_reply", "retry_attempt": idx},
            )
            if asset.generation_status == "ready":
                logger.info(
                    "photo_retry_success",
                    attempt=idx,
                    provider=provider,
                    persona=persona.display_name,
                    asset_id=str(asset.id),
                    used_reference_image=asset.metadata_json.get("used_reference_image"),
                    revised_prompt_preview=truncate_text(asset.metadata_json.get("revised_prompt") or "", 120),
                    provider_asset_id=asset.provider_asset_id,
                )
                return [asset]
            logger.info(
                "photo_retry_failed_attempt",
                attempt=idx,
                provider=provider,
                persona=persona.display_name,
                asset_id=str(asset.id),
                scene_hint_preview=truncate_text(spec["scene_hint"], 100),
                use_reference_image=bool(spec["use_reference_image"]),
                generation_error_type=asset.metadata_json.get("generation_error_type"),
                error_preview=truncate_text(
                    asset.metadata_json.get("generation_error_repr")
                    or asset.metadata_json.get("generation_error")
                    or asset.error_message
                    or "",
                    140,
                ),
                used_reference_image=asset.metadata_json.get("used_reference_image"),
            )
            if idx == 1:
                first_error = asset.error_message
                await self._send_photo_retry_notice(
                    session,
                    user=user,
                    persona=persona,
                    conversation=conversation,
                    provider=provider,
                    body=await self._generate_photo_status_reply(
                        session,
                        user=user,
                        persona=persona,
                        conversation=conversation,
                        inbound_text=inbound_text,
                        config=config,
                        mode="retry",
                        error_message=first_error,
                    ),
                )
        logger.info(
            "photo_retry_exhausted",
            attempts=len(attempt_specs),
            provider=provider,
            persona=persona.display_name,
            inbound_text_preview=truncate_text(inbound_text, 100),
            first_error_preview=truncate_text(first_error or "", 120),
        )
        return None

    async def _send_photo_retry_notice(
        self,
        session: AsyncSession,
        *,
        user: User,
        persona: Persona | None,
        conversation: Conversation,
        provider: str,
        body: str,
    ) -> None:
        if provider == "simulator":
            return
        await self.send_outbound_message(
            session,
            user=user,
            persona=persona,
            conversation=conversation,
            body=body,
            is_proactive=False,
            skip_schedule_check=True,
        )

    async def _generate_photo_status_reply(
        self,
        session: AsyncSession,
        *,
        user: User,
        persona: Persona | None,
        conversation: Conversation,
        inbound_text: str,
        config: dict[str, Any],
        mode: str,
        error_message: str | None = None,
    ) -> str:
        if not self.openai_provider.enabled:
            fallback_map = {
                "ack": "ok hang on lol",
                "retry": "wait it glitched, trying again",
                "success": "here u go :)",
                "failure": "ugh i thought i had it but it didn't go through",
            }
            return fallback_map.get(mode, "ok")
        context = {
            "user": user,
            "persona": persona,
            "conversation": conversation,
            "inbound_message": Message(body=inbound_text),
            "recent_messages": [],
            "memory_hits": [],
            "config": config,
            **(
                await self.daily_life_service.prompt_context(
                    session,
                    user=user,
                    persona=persona,
                    config=config,
                )
            ),
        }
        instructions = await self.prompt_service.render(session, "system_prompt", context)
        prompts = {
            "ack": (
                "The user asked you to send a picture. Write one very short in-character text saying you're grabbing it now. "
                "Keep it casual, natural, and under 60 characters."
            ),
            "retry": (
                "You tried to send the requested picture once and it failed. "
                f"Error context: {error_message or 'unknown error'}. "
                "Write one short in-character text saying you're trying again in a simpler/safer way. "
                "Keep it casual and under 90 characters."
            ),
            "success": (
                "You successfully sent the requested picture. "
                "Write one very short in-character caption to go with it. Keep it casual and under 50 characters."
            ),
            "failure": (
                "You tried to send a requested picture three times and it didn't work. "
                "Write one short casual text in character explaining that the pic didn't go through, "
                "with a lightly playful vibe. Keep it under 140 characters."
            ),
        }
        response = await self.openai_provider.generate_text(
            instructions=instructions,
            input_items=[
                {
                    "role": "user",
                    "content": prompts[mode],
                }
            ],
            max_output_tokens=80,
            temperature=self.settings.openai.temperature,
        )
        fallback_map = {
            "ack": "ok hang on lol",
            "retry": "wait i'm trying again",
            "success": "here :)",
            "failure": "ugh i thought i had it but it didn't go through",
        }
        max_len_map = {"ack": 60, "retry": 90, "success": 50, "failure": 140}
        return truncate_text(response.text.strip() or fallback_map[mode], max_len_map[mode])

    def _photo_attempt_specs(
        self,
        inbound_text: str,
        persona: Persona,
        *,
        scene_hint: str | None,
        include_person: bool | None,
    ) -> list[dict[str, Any]]:
        base_hint = scene_hint or self._reactive_image_scene_hint(inbound_text, persona)
        prefer_reference = bool(include_person)
        return [
            {"scene_hint": base_hint, "use_reference_image": prefer_reference},
            {
                "scene_hint": self._safer_photo_scene_hint(inbound_text, level=1, include_person=prefer_reference),
                "use_reference_image": prefer_reference,
            },
            {
                "scene_hint": self._safer_photo_scene_hint(inbound_text, level=2, include_person=False),
                "use_reference_image": False,
            },
        ]

    def _safer_photo_scene_hint(self, inbound_text: str, level: int, include_person: bool) -> str:
        normalized = normalize_text(inbound_text)
        if "sunset" in normalized or "sky" in normalized:
            if include_person:
                if level == 1:
                    return "same person in a simple golden-hour photo near a window, soft sunset colors, casual everyday vibe"
                return "same person in soft evening light, simple background, warm sky tones, relaxed phone photo"
            if level == 1:
                return "simple sunset sky photo, mostly sky and clouds, soft pink and orange colors, relaxed phone-camera feel"
            return "quiet evening sky over trees, simple landscape, soft warm colors, everyday phone photo"
        if "food" in normalized or "snack" in normalized or "meal" in normalized or "eating" in normalized:
            if include_person:
                if level == 1:
                    return "same person with a simple snack photo, casual table setting, cozy everyday phone-photo feel"
                return "same person enjoying a casual snack, soft daylight, relaxed everyday vibe"
            if level == 1:
                return "simple food snapshot on a table, cozy everyday phone-photo feel"
            return "soft daylight snack photo, simple composition, casual everyday vibe"
        if include_person:
            if level == 1:
                return "same person in a simple casual everyday photo, fully clothed, wholesome vibe, soft daylight"
            return "same person in a very simple wholesome phone photo, no dramatic styling, relaxed everyday look"
        if level == 1:
            return "simple casual everyday photo, fully clothed, wholesome vibe, plain background, soft daylight"
        return "very simple wholesome phone photo, no dramatic styling, soft daylight, relaxed everyday look"

    def _reactive_image_scene_hint(self, inbound_text: str, persona: Persona) -> str:
        normalized = normalize_text(inbound_text)
        if "what are you wearing" in normalized or "outfit" in normalized:
            return "casual outfit photo in soft natural light, candid and wholesome, lightly stylized phone-photo feel"
        if "selfie" in normalized:
            return "casual selfie in soft daylight, candid and wholesome, lightly stylized phone-photo feel"
        if "what did yours look like" in normalized or "do u have a pic" in normalized or "do you have a pic" in normalized:
            return "pretty sunset sky photo with a soft candid phone-photo look"
        if "outside my window" in normalized or "out my window" in normalized or "view" in normalized or "scene" in normalized:
            return "pretty outside view from a window, cozy everyday moment, soft natural light"
        if "sunset" in normalized or "sky" in normalized:
            return "soft outdoor sky scene with a warm candid feel, lightly stylized phone-photo look"
        if "eating" in normalized or "food" in normalized or "snack" in normalized or "meal" in normalized:
            return "cute candid food photo with a cozy everyday vibe, lightly stylized phone-photo feel"
        if "photo" in normalized or "picture" in normalized or "pic" in normalized or "look at this" in normalized or "show me" in normalized:
            return "cute everyday photo with a relaxed, candid, lightly stylized phone-photo vibe"
        favorites = persona.favorite_activities or ["cozy everyday moment at home"]
        return random.choice(favorites)

    async def generate_reply(
        self,
        session: AsyncSession,
        *,
        user: User,
        persona: Persona | None,
        conversation: Conversation,
        inbound_message: Message,
        recent_messages: list[Message],
        config: dict[str, Any],
    ) -> str:
        memory_hits = await self.memory_service.retrieve(
            session,
            user_id=user.id,
            persona_id=persona.id if persona else None,
            query=inbound_message.body or "",
            top_k=int(config["memory"]["top_k"]),
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
            "inbound_message": inbound_message,
            "recent_messages": recent_messages,
            "memory_hits": memory_hits,
            "config": config,
            **daily_context,
        }
        instructions = await self.prompt_service.render(session, "system_prompt", context)
        user_prompt = await self.prompt_service.render(session, "reactive_reply", context)
        if not self.openai_provider.enabled:
            fallback = "I’m here with you. I can’t fully generate a response right now, but I’m still listening."
            return truncate_text(fallback, int(config["messaging"]["max_message_length"]))
        response = await self.openai_provider.generate_text(
            instructions=instructions,
            input_items=[{"role": "user", "content": user_prompt}],
            max_output_tokens=self.settings.openai.max_output_tokens,
            temperature=self.settings.openai.temperature,
        )
        reply = response.text.strip()
        reply = truncate_text(reply, int(config["messaging"]["max_message_length"]))

        outbound_safety = await self.safety_service.validate_outbound(
            session,
            text=reply,
            user=user,
            persona=persona,
            conversation=conversation,
            config=config,
            source_message=inbound_message,
        )
        if outbound_safety.blocked and outbound_safety.safe_reply:
            reply = outbound_safety.safe_reply

        previous_outbound_stmt = (
            select(Message)
            .where(Message.conversation_id == conversation.id, Message.direction == Direction.outbound)
            .order_by(desc(Message.created_at))
            .limit(3)
        )
        previous_outbound = (await session.execute(previous_outbound_stmt)).scalars().all()
        for prior in previous_outbound:
            if similarity_score(prior.body, reply) >= float(config["messaging"]["duplicate_similarity_threshold"]):
                reply = truncate_text(
                    f"{reply} Tell me a little more about what’s on your mind right now.",
                    int(config["messaging"]["max_message_length"]),
                )
                break
        return reply

    async def send_outbound_message(
        self,
        session: AsyncSession,
        *,
        user: User,
        persona: Persona | None,
        conversation: Conversation,
        body: str,
        is_proactive: bool,
        media_assets: list[MediaAsset] | None = None,
        ignore_quiet_hours: bool = False,
        skip_schedule_check: bool = False,
    ) -> Message:
        config = await self.config_service.get_effective_config(session, user=user, persona=persona)
        if skip_schedule_check:
            decision_allowed = True
        else:
            decision = await self.schedule_service.can_send_message(
                session,
                user=user,
                config=config,
                ignore_quiet_hours=ignore_quiet_hours,
            )
            decision_allowed = decision.allowed
        status = MessageStatus.queued if decision_allowed else MessageStatus.blocked
        message = Message(
            conversation_id=conversation.id,
            user_id=user.id,
            persona_id=persona.id if persona else None,
            direction=Direction.outbound,
            channel=Channel.mms if media_assets else Channel.sms,
            provider="twilio",
            idempotency_key=make_idempotency_key(conversation.id, body, utc_now()),
            body=body,
            normalized_body=normalize_text(body),
            status=status,
            is_proactive=is_proactive,
        )
        session.add(message)
        await session.flush()

        if media_assets:
            for asset in media_assets:
                asset.message_id = message.id
        if not decision_allowed:
            return message

        if not self.twilio_provider.enabled:
            message.status = MessageStatus.failed
            session.add(
                DeliveryAttempt(
                    message_id=message.id,
                    provider="twilio",
                    attempt_number=1,
                    status=DeliveryStatus.failed,
                    error_message="Twilio is not configured",
                    request_json={"to_number": user.phone_number, "body": body},
                    response_json={},
                )
            )
            await session.flush()
            return message

        media_urls = [asset.remote_url for asset in media_assets or [] if asset.remote_url]
        if not media_urls:
            media_urls = [
                f"{self.settings.app.base_url.rstrip('/')}/media/{asset.id}"
                for asset in media_assets or []
                if asset.local_path
            ]
        result = await self.twilio_provider.send_message(
            to_number=user.phone_number,
            body=body,
            media_urls=media_urls or None,
        )
        message.provider_message_sid = result.provider_sid
        message.status = MessageStatus.sent if result.provider_sid else MessageStatus.failed
        message.sent_at = utc_now()
        attempt = DeliveryAttempt(
            message_id=message.id,
            provider="twilio",
            attempt_number=1,
            status=DeliveryStatus.sent if result.provider_sid else DeliveryStatus.failed,
            error_message=result.error_message,
            request_json={"to_number": user.phone_number, "body": body, "media_urls": media_urls},
            response_json=result.raw_response,
        )
        session.add(attempt)
        self.conversation_service.mark_outbound(user, conversation)
        await session.flush()
        return message

    async def update_delivery_status(
        self,
        session: AsyncSession,
        *,
        provider_sid: str,
        message_status: str,
        payload: dict[str, Any],
    ) -> Message | None:
        stmt = select(Message).where(Message.provider_message_sid == provider_sid)
        message = (await session.execute(stmt)).scalar_one_or_none()
        if message is None:
            return None
        status_map = {
            "queued": MessageStatus.queued,
            "sent": MessageStatus.sent,
            "delivered": MessageStatus.delivered,
            "failed": MessageStatus.failed,
            "undelivered": MessageStatus.failed,
        }
        message.status = status_map.get(message_status, message.status)
        if message.status == MessageStatus.delivered:
            message.delivered_at = utc_now()
        attempt = DeliveryAttempt(
            message_id=message.id,
            provider="twilio",
            attempt_number=2,
            status=DeliveryStatus.acknowledged,
            request_json={},
            response_json=payload,
        )
        session.add(attempt)
        await session.flush()
        return message
