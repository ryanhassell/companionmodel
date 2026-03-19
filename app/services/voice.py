from __future__ import annotations

import html
from typing import Any

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.settings import RuntimeSettings
from app.models.communication import CallRecord
from app.models.enums import CallDirection, CallStatus
from app.models.persona import Persona
from app.models.user import User
from app.providers.openai import OpenAIProvider
from app.providers.twilio import TwilioProvider
from app.services.prompt import PromptService


class VoiceService:
    def __init__(
        self,
        settings: RuntimeSettings,
        twilio_provider: TwilioProvider,
        openai_provider: OpenAIProvider,
        prompt_service: PromptService,
    ) -> None:
        self.settings = settings
        self.twilio_provider = twilio_provider
        self.openai_provider = openai_provider
        self.prompt_service = prompt_service

    async def initiate_call(
        self,
        session: AsyncSession,
        *,
        user: User,
        persona: Persona | None,
        config: dict[str, Any],
        opening_line: str | None = None,
    ) -> CallRecord:
        if not self.settings.voice.enabled:
            raise RuntimeError("Voice calling is disabled")
        script = await self.generate_script(
            session,
            user=user,
            persona=persona,
            config=config,
            opening_line=opening_line,
        )
        record = CallRecord(
            user_id=user.id,
            persona_id=persona.id if persona else None,
            direction=CallDirection.outbound,
            status=CallStatus.queued,
            to_number=user.phone_number,
            from_number=self.settings.twilio.from_number,
            script=script,
        )
        session.add(record)
        await session.flush()
        result = await self.twilio_provider.initiate_call(
            to_number=user.phone_number,
            twiml_url=f"{self.settings.app.public_webhook_base_url.rstrip('/')}/webhooks/twilio/voice?call_id={record.id}",
        )
        record.provider_call_sid = result.provider_sid
        try:
            record.status = CallStatus(result.status)
        except ValueError:
            record.status = CallStatus.queued
        record.metadata_json = result.raw_response
        await session.flush()
        return record

    async def generate_script(
        self,
        session: AsyncSession,
        *,
        user: User,
        persona: Persona | None,
        config: dict[str, Any],
        opening_line: str | None = None,
    ) -> str:
        context = {
            "user": user,
            "persona": persona,
            "config": config,
            "opening_line": opening_line or "",
        }
        rendered = await self.prompt_service.render(session, "call_script", context)
        if not self.openai_provider.enabled:
            return opening_line or "Hi, I wanted to check in and say hello."
        response = await self.openai_provider.generate_text(
            instructions="Write a brief, calm, safe call script for a voice assistant.",
            input_items=[{"role": "user", "content": rendered}],
            max_output_tokens=220,
        )
        return response.text

    def build_twiml(self, script: str) -> str:
        safe_script = html.escape(script)
        return (
            '<?xml version="1.0" encoding="UTF-8"?>'
            "<Response>"
            f'<Say voice="alice">{safe_script}</Say>'
            "<Pause length=\"1\"/>"
            "<Hangup/>"
            "</Response>"
        )

    async def update_call_status(self, session: AsyncSession, *, provider_sid: str, status: str) -> CallRecord | None:
        stmt = select(CallRecord).where(CallRecord.provider_call_sid == provider_sid)
        record = (await session.execute(stmt)).scalar_one_or_none()
        if record is None:
            return None
        try:
            record.status = CallStatus(status)
        except ValueError:
            record.status = CallStatus.failed
        await session.flush()
        return record
