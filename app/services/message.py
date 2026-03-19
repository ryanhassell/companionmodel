from __future__ import annotations

from datetime import timedelta
from typing import Any

from sqlalchemy import desc, func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.settings import RuntimeSettings
from app.models.communication import Conversation, DeliveryAttempt, MediaAsset, Message
from app.models.enums import Channel, DeliveryStatus, Direction, MediaRole, MessageStatus
from app.models.persona import Persona
from app.models.user import User
from app.providers.base import InboundMessagePayload
from app.providers.openai import OpenAIProvider
from app.providers.twilio import TwilioProvider
from app.services.config import ConfigService
from app.services.conversation import ConversationService
from app.services.image import ImageService
from app.services.memory import MemoryService
from app.services.prompt import PromptService
from app.services.safety import SafetyService
from app.services.schedule import ScheduleService
from app.utils.text import make_idempotency_key, normalize_text, similarity_score, truncate_text
from app.utils.time import utc_now


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
        reply_text = safety.safe_reply or await self.generate_reply(
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
        context = {
            "user": user,
            "persona": persona,
            "conversation": conversation,
            "inbound_message": inbound_message,
            "recent_messages": recent_messages,
            "memory_hits": memory_hits,
            "config": config,
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
    ) -> Message:
        config = await self.config_service.get_effective_config(session, user=user, persona=persona)
        decision = await self.schedule_service.can_send_message(
            session,
            user=user,
            config=config,
            ignore_quiet_hours=ignore_quiet_hours,
        )
        status = MessageStatus.queued if decision.allowed else MessageStatus.blocked
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
        if not decision.allowed:
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
