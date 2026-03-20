from __future__ import annotations

import asyncio
import audioop
import base64
import html
import io
import json
import uuid
import wave
from contextlib import suppress
from dataclasses import dataclass
from datetime import datetime
from typing import Any
from urllib.parse import quote

from fastapi import WebSocket, WebSocketDisconnect
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.logging import get_logger
from app.core.settings import RuntimeSettings
from app.db.session import get_sessionmaker
from app.models.communication import CallRecord, Conversation, Message
from app.models.enums import CallDirection, CallStatus, Channel, Direction, MemoryType, MessageStatus
from app.models.memory import MemoryItem
from app.models.persona import Persona
from app.models.user import User
from app.providers.elevenlabs import ElevenLabsProvider
from app.providers.openai import OpenAIProvider
from app.providers.twilio import TwilioProvider
from app.services.daily_life import DailyLifeService
from app.services.memory import MemoryService
from app.services.prompt import PromptService
from app.utils.time import utc_now

logger = get_logger(__name__)


@dataclass(slots=True)
class RealtimeSessionOutcome:
    transcript: str
    started_at: datetime | None
    ended_at: datetime | None
    tool_events: list[dict[str, Any]]
    session_events: list[dict[str, Any]]
    ended_by_tool: bool
    end_reason: str | None


@dataclass(slots=True)
class MediaStreamSessionOutcome:
    transcript: str
    started_at: datetime | None
    ended_at: datetime | None
    session_events: list[dict[str, Any]]
    end_reason: str | None


class VoiceService:
    def __init__(
        self,
        settings: RuntimeSettings,
        twilio_provider: TwilioProvider,
        openai_provider: OpenAIProvider,
        elevenlabs_provider: ElevenLabsProvider,
        prompt_service: PromptService,
        memory_service: MemoryService,
        daily_life_service: DailyLifeService,
    ) -> None:
        self.settings = settings
        self.twilio_provider = twilio_provider
        self.openai_provider = openai_provider
        self.elevenlabs_provider = elevenlabs_provider
        self.prompt_service = prompt_service
        self.memory_service = memory_service
        self.daily_life_service = daily_life_service
        self._sessionmaker = get_sessionmaker()
        self._session_tasks: dict[str, asyncio.Task[None]] = {}

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
        if self._voice_driver() == "openai_realtime_sip":
            return await self.initiate_realtime_call(
                session,
                user=user,
                persona=persona,
                config=config,
                opening_line=opening_line,
            )
        return await self.initiate_media_stream_call(
            session,
            user=user,
            persona=persona,
            config=config,
            opening_line=opening_line,
        )

    def _voice_driver(self) -> str:
        if self.settings.voice.driver:
            return self.settings.voice.driver
        if self.settings.voice.realtime_enabled:
            return "openai_realtime_sip"
        return "twilio_twiml"

    def _media_stream_websocket_url(self) -> str:
        base = self.settings.app.public_webhook_base_url.rstrip("/")
        if base.startswith("https://"):
            base = "wss://" + base[len("https://") :]
        elif base.startswith("http://"):
            base = "ws://" + base[len("http://") :]
        return f"{base}{self.settings.voice.media_streams_websocket_path}"

    def _selected_elevenlabs_voice(self, persona: Persona | None) -> str | None:
        if persona is not None and isinstance(persona.prompt_overrides, dict):
            override = str(persona.prompt_overrides.get("elevenlabs_voice_id") or "").strip()
            if override:
                return override
        return self.settings.voice.elevenlabs_default_voice_id

    async def initiate_media_stream_call(
        self,
        session: AsyncSession,
        *,
        user: User,
        persona: Persona | None,
        config: dict[str, Any],
        opening_line: str | None = None,
    ) -> CallRecord:
        if not self._selected_elevenlabs_voice(persona):
            raise RuntimeError("No ElevenLabs voice is configured for this persona or globally")
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
            metadata_json={
                "mode": "media_streams",
                "transport": "twilio_media_streams",
                "opening_line": opening_line,
                "voice_driver": self._voice_driver(),
                "config_snapshot": {
                    "voice": config.get("voice", {}),
                    "safety": {"daily_call_cap": config.get("safety", {}).get("daily_call_cap")},
                },
            },
        )
        session.add(record)
        await session.flush()
        twiml = self.build_media_stream_twiml(
            record_id=str(record.id),
            user=user,
            persona=persona,
            opening_line=opening_line,
        )
        result = await self.twilio_provider.initiate_call(
            to_number=user.phone_number,
            twiml=twiml,
            status_callback=self.settings.twilio.voice_status_callback_url,
        )
        record.provider_call_sid = result.provider_sid
        try:
            record.status = CallStatus(result.status)
        except ValueError:
            record.status = CallStatus.queued
        record.metadata_json = {
            **(record.metadata_json or {}),
            "twilio": result.raw_response,
            "provider_sid": result.provider_sid,
        }
        await session.flush()
        logger.info(
            "media_stream_call_initiated",
            call_record_id=str(record.id),
            provider_sid=result.provider_sid,
            user_id=str(user.id),
            persona=persona.display_name if persona else None,
            transport="twilio_media_streams",
        )
        return record

    async def initiate_realtime_call(
        self,
        session: AsyncSession,
        *,
        user: User,
        persona: Persona | None,
        config: dict[str, Any],
        opening_line: str | None = None,
    ) -> CallRecord:
        sip_uri = self.settings.voice.realtime_sip_uri
        if not sip_uri:
            raise RuntimeError("VOICE_REALTIME_SIP_URI is not configured")
        record = CallRecord(
            user_id=user.id,
            persona_id=persona.id if persona else None,
            direction=CallDirection.outbound,
            status=CallStatus.queued,
            to_number=user.phone_number,
            from_number=self.settings.twilio.from_number,
            metadata_json={
                "mode": "realtime",
                "transport": "twilio_pstn_openai_sip",
                "opening_line": opening_line,
                "config_snapshot": {
                    "voice": config.get("voice", {}),
                    "safety": {"daily_call_cap": config.get("safety", {}).get("daily_call_cap")},
                },
            },
        )
        session.add(record)
        await session.flush()
        twiml = self.build_realtime_bridge_twiml(
            record_id=str(record.id),
            user=user,
            persona=persona,
            sip_uri=sip_uri,
        )
        result = await self.twilio_provider.initiate_call(
            to_number=user.phone_number,
            twiml=twiml,
            status_callback=self.settings.twilio.voice_status_callback_url,
        )
        record.provider_call_sid = result.provider_sid
        try:
            record.status = CallStatus(result.status)
        except ValueError:
            record.status = CallStatus.queued
        record.metadata_json = {
            **(record.metadata_json or {}),
            "twilio": result.raw_response,
            "provider_sid": result.provider_sid,
        }
        await session.flush()
        logger.info(
            "realtime_call_initiated",
            call_record_id=str(record.id),
            provider_sid=result.provider_sid,
            user_id=str(user.id),
            persona=persona.display_name if persona else None,
            transport="twilio_pstn_openai_sip",
        )
        return record

    def build_realtime_bridge_twiml(
        self,
        *,
        record_id: str,
        user: User,
        persona: Persona | None,
        sip_uri: str,
    ) -> str:
        base_sip = sip_uri.rstrip("?")
        query_bits = [
            f"x-record-id={quote(record_id)}",
            f"x-user-id={quote(str(user.id))}",
            f"x-user-phone={quote(user.phone_number)}",
        ]
        if persona is not None:
            query_bits.append(f"x-persona-id={quote(str(persona.id))}")
        separator = "&" if "?" in base_sip else "?"
        final_sip = f"{base_sip}{separator}{'&'.join(query_bits)}"
        return (
            '<?xml version="1.0" encoding="UTF-8"?>'
            "<Response>"
            '<Dial answerOnBridge="true">'
            f"<Sip>{html.escape(final_sip)}</Sip>"
            "</Dial>"
            "</Response>"
        )

    def build_media_stream_twiml(
        self,
        *,
        record_id: str,
        user: User,
        persona: Persona | None,
        opening_line: str | None = None,
    ) -> str:
        websocket_url = html.escape(self._media_stream_websocket_url())
        params = {
            "call_record_id": record_id,
            "user_id": str(user.id),
            "user_phone": user.phone_number,
            "direction": "outbound",
        }
        if persona is not None:
            params["persona_id"] = str(persona.id)
        if opening_line:
            params["opening_line"] = opening_line
        parameter_xml = "".join(
            f'<Parameter name="{html.escape(name)}" value="{html.escape(value)}"/>' for name, value in params.items()
        )
        return (
            '<?xml version="1.0" encoding="UTF-8"?>'
            "<Response>"
            "<Connect>"
            f'<Stream url="{websocket_url}">{parameter_xml}</Stream>'
            "</Connect>"
            "</Response>"
        )

    def build_hangup_twiml(self) -> str:
        return '<?xml version="1.0" encoding="UTF-8"?><Response><Hangup/></Response>'

    def _selected_realtime_voice(self, persona: Persona | None) -> str:
        if persona is not None and isinstance(persona.prompt_overrides, dict):
            override = str(persona.prompt_overrides.get("realtime_voice") or "").strip()
            if override:
                return override
        return self.settings.voice.realtime_voice or self.settings.voice.default_voice

    def _turn_detection_payload(self) -> dict[str, Any]:
        return {
            "type": "semantic_vad",
            "eagerness": "low",
            "create_response": True,
            "interrupt_response": False,
        }

    async def handle_openai_realtime_event(
        self,
        session: AsyncSession,
        *,
        payload: dict[str, Any],
    ) -> dict[str, Any]:
        event_type = _event_value(payload, "type", "event", "event_type")
        call_id = _nested_value(payload, "call_id", "data.call_id", "call.id", "data.id")
        if not call_id:
            raise RuntimeError("Missing call_id in realtime webhook payload")
        if event_type in {"realtime.call.incoming", "call.incoming"}:
            return await self._handle_incoming_call(session, call_id=call_id, payload=payload)
        if event_type in {"realtime.call.ended", "call.ended"}:
            return await self._handle_call_ended(session, call_id=call_id, payload=payload)
        logger.info("realtime_webhook_ignored", event_type=event_type, call_id=call_id)
        return {"status": "ignored", "event_type": event_type, "call_id": call_id}

    async def _handle_incoming_call(
        self,
        session: AsyncSession,
        *,
        call_id: str,
        payload: dict[str, Any],
    ) -> dict[str, Any]:
        record = await self._resolve_call_record(session, payload=payload)
        if record is None:
            user = await self._resolve_user_from_payload(session, payload)
            if user is None:
                logger.info(
                    "realtime_call_rejected_unknown_caller",
                    call_id=call_id,
                    from_number=_nested_value(payload, "from", "data.from", "caller", "call.from"),
                )
                with suppress(Exception):
                    await self.openai_provider.end_realtime_call(call_id)
                return {"status": "rejected", "reason": "unknown_caller", "call_id": call_id}
            persona = await self._resolve_persona_from_payload(session, payload, user=user)
            record = CallRecord(
                user_id=user.id if user else None,
                persona_id=persona.id if persona else None,
                direction=CallDirection.inbound,
                status=CallStatus.ringing,
                from_number=_nested_value(payload, "from", "data.from", "caller", "call.from"),
                to_number=_nested_value(payload, "to", "data.to", "call.to"),
                metadata_json={
                    "mode": "realtime",
                    "transport": "openai_sip_inbound",
                    "openai_payload": payload,
                },
            )
            session.add(record)
            await session.flush()
        else:
            record.status = CallStatus.ringing
            record.metadata_json = {**(record.metadata_json or {}), "openai_payload": payload}
        record.provider_call_sid = call_id
        user = await session.get(User, record.user_id) if record.user_id else None
        persona = await session.get(Persona, record.persona_id) if record.persona_id else None
        config = {
            "voice": self.settings.voice.model_dump(mode="json"),
            "memory": self.settings.memory.model_dump(mode="json"),
            "app": {"timezone": user.timezone if user else self.settings.app.timezone},
        }
        instructions = await self._build_realtime_session_prompt(
            session,
            user=user,
            persona=persona,
            call_record=record,
            config=config,
            lightweight=True,
        )
        accepted = await self.openai_provider.accept_realtime_call(
            call_id,
            payload={
                "type": "realtime",
                "model": self.settings.openai.realtime_model,
                "instructions": instructions,
                "audio": {
                    "output": {
                        "voice": self._selected_realtime_voice(persona),
                    }
                },
            },
        )
        record.metadata_json = {
            **(record.metadata_json or {}),
            "openai_call_id": call_id,
            "accept_response": accepted,
        }
        await session.flush()
        self._schedule_sideband_session(str(record.id), call_id)
        logger.info(
            "realtime_call_accepted",
            call_record_id=str(record.id),
            call_id=call_id,
            user_id=str(record.user_id) if record.user_id else None,
            persona_id=str(record.persona_id) if record.persona_id else None,
        )
        return {"status": "accepted", "call_record_id": str(record.id), "call_id": call_id}

    async def _handle_call_ended(
        self,
        session: AsyncSession,
        *,
        call_id: str,
        payload: dict[str, Any],
    ) -> dict[str, Any]:
        stmt = select(CallRecord).where(CallRecord.provider_call_sid == call_id)
        record = (await session.execute(stmt)).scalar_one_or_none()
        if record is None:
            return {"status": "missing", "call_id": call_id}
        if record.status != CallStatus.completed:
            record.status = CallStatus.completed
        record.ended_at = utc_now()
        record.metadata_json = {**(record.metadata_json or {}), "end_event": payload}
        await session.flush()
        return {"status": "updated", "call_id": call_id, "call_record_id": str(record.id)}

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
            '<Pause length="1"/>'
            "<Hangup/>"
            "</Response>"
        )

    async def update_call_status(
        self,
        session: AsyncSession,
        *,
        provider_sid: str,
        status: str,
        payload: dict[str, Any] | None = None,
    ) -> CallRecord | None:
        stmt = select(CallRecord).where(CallRecord.provider_call_sid == provider_sid)
        record = (await session.execute(stmt)).scalar_one_or_none()
        if record is None:
            return None
        try:
            record.status = CallStatus(status)
        except ValueError:
            record.status = _coerce_call_status(status)
        if record.status == CallStatus.in_progress and record.started_at is None:
            record.started_at = utc_now()
        if record.status in {CallStatus.completed, CallStatus.failed, CallStatus.no_answer}:
            record.ended_at = utc_now()
        record.metadata_json = {
            **(record.metadata_json or {}),
            "twilio_status": payload or {"status": status},
        }
        await session.flush()
        return record

    async def handle_twilio_voice_webhook(self, session: AsyncSession, *, form: dict[str, Any]) -> str:
        if self._voice_driver() != "twilio_media_streams_openai_stt_elevenlabs":
            call_id = str(form.get("call_id") or form.get("CallSid") or "")
            if not call_id:
                return self.build_hangup_twiml()
            record = await session.get(CallRecord, call_id)
            if record is None:
                stmt = select(CallRecord).where(CallRecord.provider_call_sid == call_id)
                record = (await session.execute(stmt)).scalar_one_or_none()
            if record is None:
                return self.build_hangup_twiml()
            script = record.script or "Hi, just checking in and saying hello."
            return self.build_twiml(script)

        incoming_phone = _normalize_phone_number(str(form.get("From") or ""))
        user = await self._resolve_user_from_payload(session, {"from": incoming_phone})
        if user is None:
            logger.info("twilio_voice_rejected_unknown_caller", from_number=incoming_phone)
            return self.build_hangup_twiml()
        persona = await self._resolve_persona_from_payload(session, {"from": incoming_phone}, user=user)
        call_sid = str(form.get("CallSid") or "")
        stmt = select(CallRecord).where(CallRecord.provider_call_sid == call_sid)
        record = (await session.execute(stmt)).scalar_one_or_none()
        if record is None:
            record = CallRecord(
                user_id=user.id,
                persona_id=persona.id if persona else None,
                direction=CallDirection.inbound,
                status=CallStatus.ringing,
                from_number=incoming_phone,
                to_number=str(form.get("To") or "") or self.settings.twilio.from_number,
                provider_call_sid=call_sid,
                metadata_json={
                    "mode": "media_streams",
                    "transport": "twilio_media_streams",
                    "voice_driver": self._voice_driver(),
                    "twilio_voice_form": dict(form),
                },
            )
            session.add(record)
            await session.flush()
        else:
            record.status = CallStatus.ringing
            record.user_id = user.id
            record.persona_id = persona.id if persona else None
            record.from_number = incoming_phone
            record.to_number = str(form.get("To") or "") or record.to_number
            record.metadata_json = {**(record.metadata_json or {}), "twilio_voice_form": dict(form)}
            await session.flush()
        logger.info(
            "twilio_voice_media_stream_ready",
            call_record_id=str(record.id),
            provider_sid=call_sid,
            user_id=str(user.id),
            persona_id=str(persona.id) if persona else None,
        )
        return self.build_media_stream_twiml(
            record_id=str(record.id),
            user=user,
            persona=persona,
            opening_line=None,
        )

    def _schedule_sideband_session(self, call_record_id: str, call_id: str) -> None:
        existing = self._session_tasks.get(call_record_id)
        if existing and not existing.done():
            return
        task = asyncio.create_task(self._run_sideband_session(call_record_id=call_record_id, call_id=call_id))
        self._session_tasks[call_record_id] = task
        task.add_done_callback(lambda _: self._session_tasks.pop(call_record_id, None))

    async def _run_sideband_session(self, *, call_record_id: str, call_id: str) -> None:
        try:
            async with self._sessionmaker() as session:
                record = await session.get(CallRecord, call_record_id)
                if record is None:
                    return
                user = await session.get(User, record.user_id) if record.user_id else None
                persona = await session.get(Persona, record.persona_id) if record.persona_id else None
                config = {
                    "voice": self.settings.voice.model_dump(mode="json"),
                    "memory": self.settings.memory.model_dump(mode="json"),
                    "app": {"timezone": user.timezone if user else self.settings.app.timezone},
                }
                outcome = await self._stream_realtime_session(
                    session,
                    call_record=record,
                    user=user,
                    persona=persona,
                    config=config,
                    call_id=call_id,
                )
                await self._finalize_realtime_call(
                    session,
                    call_record=record,
                    user=user,
                    persona=persona,
                    transcript=outcome.transcript,
                    outcome=outcome,
                )
                await session.commit()
        except Exception as exc:  # pragma: no cover
            logger.exception(
                "realtime_call_session_failed",
                call_record_id=call_record_id,
                call_id=call_id,
                error=repr(exc),
            )
            async with self._sessionmaker() as session:
                record = await session.get(CallRecord, call_record_id)
                if record is not None:
                    record.status = CallStatus.failed
                    record.ended_at = utc_now()
                    record.metadata_json = {**(record.metadata_json or {}), "session_error": repr(exc)}
                    await session.commit()

    async def handle_twilio_media_stream(self, websocket: WebSocket) -> None:
        await websocket.accept()
        session_events: list[dict[str, Any]] = []
        call_record_id: str | None = None
        record: CallRecord | None = None
        user: User | None = None
        persona: Persona | None = None
        config: dict[str, Any] | None = None
        instructions = ""
        stream_sid = ""
        transcript_entries: list[tuple[str, str]] = []
        generation_counter = 0
        speech_task: asyncio.Task[None] | None = None
        utterance_buffer = bytearray()
        speech_active = False
        speech_ms = 0
        silence_ms = 0
        started_at: datetime | None = None

        async def cancel_speech() -> None:
            nonlocal speech_task, generation_counter
            generation_counter += 1
            if speech_task and not speech_task.done():
                speech_task.cancel()
                with suppress(asyncio.CancelledError):
                    await speech_task
            speech_task = None
            if stream_sid:
                await websocket.send_json({"event": "clear", "streamSid": stream_sid})

        async def speak_text(text: str, generation_id: int) -> None:
            if not text or generation_id != generation_counter:
                return
            voice_id = self._selected_elevenlabs_voice(persona)
            if not voice_id:
                return
            async for chunk in self.elevenlabs_provider.stream_tts(
                text=text,
                voice_id=voice_id,
                model_id=self.settings.voice.elevenlabs_tts_model,
                output_format="ulaw_8000",
            ):
                if generation_id != generation_counter:
                    break
                for piece in _chunk_audio_bytes(chunk, ms=self.settings.voice.stream_chunk_ms):
                    if generation_id != generation_counter:
                        break
                    await websocket.send_json(
                        {
                            "event": "media",
                            "streamSid": stream_sid,
                            "media": {"payload": base64.b64encode(piece).decode("ascii")},
                        }
                    )

        async def generate_and_speak(latest_user_text: str, generation_id: int) -> None:
            nonlocal transcript_entries
            if generation_id != generation_counter or not record or not config:
                return
            input_text = _format_call_turn_prompt(transcript_entries, latest_user_text)
            response = await self.openai_provider.generate_text(
                instructions=instructions,
                input_items=[{"role": "user", "content": input_text}],
                model=self.settings.voice.text_model,
                max_output_tokens=220,
            )
            assistant_text = response.text.strip()
            if not assistant_text or generation_id != generation_counter:
                return
            transcript_entries.append(("assistant", assistant_text))
            await speak_text(assistant_text, generation_id)

        async def finalize_user_utterance() -> None:
            nonlocal utterance_buffer, speech_active, speech_ms, silence_ms, speech_task
            audio_bytes = bytes(utterance_buffer)
            utterance_buffer = bytearray()
            speech_active = False
            speech_ms = 0
            silence_ms = 0
            if not audio_bytes or not record:
                return
            transcript = await self.openai_provider.transcribe_audio(
                audio_bytes=_mulaw_to_wav_bytes(audio_bytes),
                filename="call.wav",
                mime_type="audio/wav",
                model=self.settings.voice.stt_model,
                prompt="Phone call speech. Return only the spoken words.",
            )
            user_text = transcript.text.strip()
            if not user_text:
                return
            transcript_entries.append(("user", user_text))
            generation_id = generation_counter
            speech_task = asyncio.create_task(generate_and_speak(user_text, generation_id))

        try:
            while True:
                event = await websocket.receive_json()
                event_type = str(event.get("event") or "")
                session_events.append({"type": event_type, "timestamp": utc_now().isoformat()})
                if event_type == "start":
                    started_at = started_at or utc_now()
                    start = event.get("start") or {}
                    stream_sid = str(start.get("streamSid") or "")
                    custom_parameters = start.get("customParameters") or {}
                    call_record_id = str(custom_parameters.get("call_record_id") or "")
                    if not call_record_id:
                        await websocket.close()
                        return
                    async with self._sessionmaker() as session:
                        record = await session.get(CallRecord, call_record_id)
                        if record is None:
                            await websocket.close()
                            return
                        user = await session.get(User, record.user_id) if record.user_id else None
                        persona = await session.get(Persona, record.persona_id) if record.persona_id else None
                        config = {
                            "voice": self.settings.voice.model_dump(mode="json"),
                            "memory": self.settings.memory.model_dump(mode="json"),
                            "app": {"timezone": user.timezone if user else self.settings.app.timezone},
                        }
                        instructions = await self._build_realtime_session_prompt(
                            session,
                            user=user,
                            persona=persona,
                            call_record=record,
                            config=config,
                            lightweight=True,
                        )
                        record.status = CallStatus.in_progress
                        record.started_at = record.started_at or started_at
                        await session.commit()
                    greeting = await self._initial_greeting_text(call_record=record, user=user, persona=persona, instructions=instructions)
                    if greeting:
                        transcript_entries.append(("assistant", greeting))
                        generation_counter += 1
                        speech_task = asyncio.create_task(speak_text(greeting, generation_counter))
                    continue
                if event_type == "media":
                    media = event.get("media") or {}
                    payload = str(media.get("payload") or "")
                    if not payload:
                        continue
                    chunk = base64.b64decode(payload)
                    if not chunk:
                        continue
                    ms = max(int(len(chunk) / 8), 20)
                    rms = _mulaw_rms(chunk)
                    if rms >= self.settings.voice.vad_rms_threshold:
                        if speech_task and not speech_task.done():
                            await cancel_speech()
                        utterance_buffer.extend(chunk)
                        speech_active = True
                        speech_ms += ms
                        silence_ms = 0
                    elif speech_active:
                        utterance_buffer.extend(chunk)
                        silence_ms += ms
                        if silence_ms >= self.settings.voice.vad_silence_ms and speech_ms >= self.settings.voice.vad_min_speech_ms:
                            await finalize_user_utterance()
                    continue
                if event_type == "stop":
                    break
        except WebSocketDisconnect:
            session_events.append({"type": "disconnect", "timestamp": utc_now().isoformat()})
        finally:
            if speech_active and speech_ms >= self.settings.voice.vad_min_speech_ms:
                with suppress(Exception):
                    await finalize_user_utterance()
            if speech_task and not speech_task.done():
                with suppress(asyncio.CancelledError):
                    await speech_task
            if record and call_record_id:
                async with self._sessionmaker() as session:
                    refreshed = await session.get(CallRecord, call_record_id)
                    user = await session.get(User, refreshed.user_id) if refreshed and refreshed.user_id else None
                    persona = await session.get(Persona, refreshed.persona_id) if refreshed and refreshed.persona_id else None
                    if refreshed is not None:
                        outcome = MediaStreamSessionOutcome(
                            transcript=_flatten_transcript_entries(transcript_entries),
                            started_at=started_at,
                            ended_at=utc_now(),
                            session_events=session_events[-100:],
                            end_reason="media_stream_stopped",
                        )
                        await self._finalize_media_stream_call(
                            session,
                            call_record=refreshed,
                            user=user,
                            persona=persona,
                            outcome=outcome,
                        )
                        await session.commit()

    async def _stream_realtime_session(
        self,
        session: AsyncSession,
        *,
        call_record: CallRecord,
        user: User | None,
        persona: Persona | None,
        config: dict[str, Any],
        call_id: str,
    ) -> RealtimeSessionOutcome:
        instructions = await self._build_realtime_session_prompt(
            session,
            user=user,
            persona=persona,
            call_record=call_record,
            config=config,
            lightweight=True,
        )
        transcript_parts: list[str] = []
        tool_events: list[dict[str, Any]] = []
        session_events: list[dict[str, Any]] = []
        started_at: datetime | None = None
        ended_at: datetime | None = None
        ended_by_tool = False
        end_reason: str | None = None
        tool_roundtrips = 0

        async with self.openai_provider.open_realtime_sideband(call_id) as websocket:
            await websocket.send(json.dumps(self._session_update_payload(instructions, persona)))
            await websocket.send(json.dumps(self._initial_greeting_payload(call_record=call_record, user=user, persona=persona)))
            while True:
                raw_event = await asyncio.wait_for(
                    websocket.recv(),
                    timeout=self.settings.voice.sideband_idle_timeout_seconds,
                )
                event = json.loads(raw_event)
                event_type = str(event.get("type") or "")
                session_events.append({"type": event_type, "timestamp": utc_now().isoformat()})
                if event_type in {"session.updated", "response.created", "response.output_item.added"}:
                    continue
                if event_type in {"call.started", "session.started"}:
                    started_at = started_at or utc_now()
                    continue
                if event_type in {"conversation.item.created", "response.output_text.delta"}:
                    text_piece = _extract_transcript_text(event)
                    if text_piece:
                        transcript_parts.append(text_piece)
                    continue
                if event_type in {"response.function_call_arguments.done", "response.function_call.done", "tool.call"}:
                    tool_roundtrips += 1
                    tool_result = await self._handle_tool_call(
                        session,
                        call_record=call_record,
                        user=user,
                        persona=persona,
                        event=event,
                    )
                    tool_events.append(tool_result)
                    await websocket.send(
                        json.dumps(
                            {
                                "type": "conversation.item.create",
                                "item": {
                                    "type": "function_call_output",
                                    "call_id": tool_result.get("tool_call_id"),
                                    "output": json.dumps(tool_result.get("output", {})),
                                },
                            }
                        )
                    )
                    if tool_result.get("end_requested"):
                        ended_by_tool = True
                        end_reason = str(tool_result.get("message") or "tool_end_call")
                        break
                    await websocket.send(json.dumps({"type": "response.create"}))
                    if tool_roundtrips >= self.settings.voice.max_tool_roundtrips:
                        end_reason = "tool_roundtrip_limit"
                        break
                    continue
                if event_type in {"response.completed", "output_audio_buffer.stopped"}:
                    continue
                if event_type in {"call.ended", "session.ended"}:
                    ended_at = utc_now()
                    end_reason = event_type
                    break
        return RealtimeSessionOutcome(
            transcript=" ".join(part.strip() for part in transcript_parts if part and part.strip()).strip(),
            started_at=started_at,
            ended_at=ended_at or utc_now(),
            tool_events=tool_events,
            session_events=session_events[-100:],
            ended_by_tool=ended_by_tool,
            end_reason=end_reason,
        )

    async def _build_realtime_session_prompt(
        self,
        session: AsyncSession,
        *,
        user: User | None,
        persona: Persona | None,
        call_record: CallRecord,
        config: dict[str, Any],
        lightweight: bool = False,
    ) -> str:
        user_context = user or User(
            phone_number=call_record.to_number or call_record.from_number or "",
            timezone=self.settings.app.timezone,
        )
        daily_context = await self.daily_life_service.prompt_context(
            session,
            user=user_context,
            persona=persona,
            config=config,
            ensure_state=not lightweight,
        )
        memory_hits = []
        if user is not None and not lightweight:
            memory_hits = await self.memory_service.retrieve(
                session,
                user_id=user.id,
                persona_id=persona.id if persona else None,
                query="phone call context, relevant memories, recurring people, current plans",
                top_k=6,
                threshold=float(self.settings.memory.similarity_threshold),
            )
        recent_messages: list[Message] = []
        if user is not None and persona is not None:
            stmt = (
                select(Conversation)
                .where(Conversation.user_id == user.id, Conversation.persona_id == persona.id)
                .order_by(Conversation.updated_at.desc())
                .limit(1)
            )
            conversation = (await session.execute(stmt)).scalar_one_or_none()
            if conversation is not None:
                recent_messages = list(
                    (
                        await session.execute(
                            select(Message)
                            .where(Message.conversation_id == conversation.id)
                            .order_by(Message.created_at.desc())
                            .limit(8)
                        )
                    ).scalars().all()
                )
                recent_messages.reverse()
        context = {
            "user": user_context,
            "persona": persona,
            "config": config,
            "call_record": call_record,
            "opening_line": (call_record.metadata_json or {}).get("opening_line") or "",
            "recent_messages": recent_messages,
            "memory_hits": memory_hits,
            **daily_context,
        }
        return await self.prompt_service.render(session, "realtime_call_session", context)

    def _session_update_payload(self, instructions: str, persona: Persona | None) -> dict[str, Any]:
        return {
            "type": "session.update",
            "session": {
                "type": "realtime",
                "model": self.settings.openai.realtime_model,
                "instructions": instructions,
                "output_modalities": ["audio"],
                "audio": {
                    "input": {
                        "turn_detection": self._turn_detection_payload(),
                    },
                    "output": {
                        "voice": self._selected_realtime_voice(persona),
                    },
                },
                "tool_choice": "auto",
                "tools": [
                    {
                        "type": "function",
                        "name": "get_recent_context",
                        "description": "Fetch recent messages, memories, and current daily-life context for this caller.",
                        "parameters": {
                            "type": "object",
                            "properties": {
                                "query": {"type": "string"},
                            },
                        },
                    },
                    {
                        "type": "function",
                        "name": "save_call_memory",
                        "description": "Save an explicit memory worth remembering from the call.",
                        "parameters": {
                            "type": "object",
                            "properties": {
                                "title": {"type": "string"},
                                "content": {"type": "string"},
                                "summary": {"type": "string"},
                                "memory_type": {"type": "string"},
                                "tags": {"type": "array", "items": {"type": "string"}},
                                "entity_name": {"type": "string"},
                                "entity_kind": {"type": "string"},
                            },
                            "required": ["content"],
                        },
                    },
                    {
                        "type": "function",
                        "name": "end_call",
                        "description": "Gracefully end the call when it makes sense.",
                        "parameters": {
                            "type": "object",
                            "properties": {
                                "reason": {"type": "string"},
                            },
                        },
                    },
                ],
            },
        }

    def _initial_greeting_payload(
        self,
        *,
        call_record: CallRecord,
        user: User | None,
        persona: Persona | None,
    ) -> dict[str, Any]:
        user_name = (user.display_name or "").strip() if user and user.display_name else ""
        opening_line = str((call_record.metadata_json or {}).get("opening_line") or "").strip()
        persona_name = persona.display_name if persona else "Companion"
        persona_style = (persona.style or "").strip() if persona and persona.style else ""
        persona_tone = (persona.tone or "").strip() if persona and persona.tone else ""
        persona_descriptor_parts = [part for part in [persona_style, persona_tone] if part]
        persona_descriptor = "; ".join(persona_descriptor_parts) if persona_descriptor_parts else "natural, casual, fully in character"
        if call_record.direction == CallDirection.inbound:
            first_line = f"hi {user_name}" if user_name else "hello?"
            prompt = (
                f"You are {persona_name}. Stay fully in character for this entire call. "
                f"Your vibe should feel {persona_descriptor}. "
                f'Say exactly this first spoken line and nothing else yet: "{first_line}". '
                "After saying it, stop and wait for the caller. "
                "Do not add a follow-up question. "
                "Do not say things like 'how can I help you today' or anything customer-service-like. "
                "Do not sound like an assistant, receptionist, or support agent."
            )
        else:
            prompt = (
                f"You are {persona_name}. Stay fully in character for this entire call. "
                f"Your vibe should feel {persona_descriptor}. "
                "You just placed this call. "
                "Start naturally like a real person making a casual call. "
                "Briefly greet the user, then smoothly ask or say the main thing you called about. "
                "Keep it to one or two short spoken sentences. "
                "Do not sound like an assistant, receptionist, or support agent."
            )
            if user_name:
                prompt += f" The user's name is {user_name}."
            if opening_line:
                prompt += f" The main point to lead with is: {opening_line}"
            else:
                prompt += (
                    f" Sound like {persona_name} calling to check in, not a scripted assistant."
                )
        return {
            "type": "response.create",
            "response": {
                "instructions": prompt
            },
        }

    async def _handle_tool_call(
        self,
        session: AsyncSession,
        *,
        call_record: CallRecord,
        user: User | None,
        persona: Persona | None,
        event: dict[str, Any],
    ) -> dict[str, Any]:
        tool_name = _nested_value(event, "name", "function.name", "item.name") or ""
        tool_call_id = _nested_value(event, "call_id", "item.call_id", "id")
        args = _parse_tool_args(event)
        if tool_name == "get_recent_context":
            output = await self._tool_get_recent_context(
                session,
                user=user,
                persona=persona,
                query=str(args.get("query") or "call context"),
            )
            return {"tool_name": tool_name, "tool_call_id": tool_call_id, "output": output, "end_requested": False}
        if tool_name == "save_call_memory":
            output = await self._tool_save_call_memory(
                session,
                call_record=call_record,
                user=user,
                persona=persona,
                args=args,
            )
            return {"tool_name": tool_name, "tool_call_id": tool_call_id, "output": output, "end_requested": False}
        if tool_name == "end_call":
            reason = str(args.get("reason") or "The conversation wrapped naturally.")
            return {
                "tool_name": tool_name,
                "tool_call_id": tool_call_id,
                "output": {"ok": True, "reason": reason},
                "message": reason,
                "end_requested": True,
            }
        return {
            "tool_name": tool_name,
            "tool_call_id": tool_call_id,
            "output": {"ok": False, "error": f"Unknown tool: {tool_name}"},
            "end_requested": False,
        }

    async def _tool_get_recent_context(
        self,
        session: AsyncSession,
        *,
        user: User | None,
        persona: Persona | None,
        query: str,
    ) -> dict[str, Any]:
        if user is None:
            return {"memories": [], "recent_messages": [], "daily_life": {}}
        config = {"memory": self.settings.memory.model_dump(mode="json")}
        daily_life = await self.daily_life_service.prompt_context(
            session,
            user=user,
            persona=persona,
            config=config,
        )
        memories = await self.memory_service.retrieve(
            session,
            user_id=user.id,
            persona_id=persona.id if persona else None,
            query=query,
            top_k=5,
            threshold=float(self.settings.memory.similarity_threshold),
        )
        stmt = select(Message).where(Message.user_id == user.id).order_by(Message.created_at.desc()).limit(6)
        messages = list((await session.execute(stmt)).scalars().all())
        messages.reverse()
        return {
            "memories": [
                {
                    "title": item.memory.title,
                    "summary": item.memory.summary,
                    "content": item.memory.content,
                    "score": round(item.score, 3),
                }
                for item in memories
            ],
            "recent_messages": [
                {
                    "direction": message.direction.value,
                    "body": message.body,
                    "created_at": message.created_at.isoformat() if message.created_at else None,
                }
                for message in messages
                if message.body
            ],
            "daily_life": {
                "current_local_datetime": daily_life.get("current_local_datetime"),
                "today": daily_life.get("today_companion_facts", []),
                "upcoming": daily_life.get("upcoming_companion_plans", []),
            },
        }

    async def _tool_save_call_memory(
        self,
        session: AsyncSession,
        *,
        call_record: CallRecord,
        user: User | None,
        persona: Persona | None,
        args: dict[str, Any],
    ) -> dict[str, Any]:
        if user is None:
            return {"ok": False, "error": "No user linked to call"}
        memory_type_raw = str(args.get("memory_type") or "fact")
        try:
            memory_type = MemoryType(memory_type_raw)
        except ValueError:
            memory_type = MemoryType.fact
        metadata = {"source": "call_tool", "call_record_id": str(call_record.id)}
        entity_name = str(args.get("entity_name") or "").strip()
        entity_kind = str(args.get("entity_kind") or "").strip()
        if entity_name:
            metadata.update(
                {
                    "entity_name": entity_name,
                    "entity_name_normalized": entity_name.casefold(),
                    "entity_kind": entity_kind or "topic",
                    "memory_scope": "entity",
                }
            )
        item = MemoryItem(
            user_id=user.id,
            persona_id=persona.id if persona else None,
            memory_type=memory_type,
            title=str(args.get("title") or "Call memory")[:120],
            content=str(args.get("content") or "").strip(),
            summary=str(args.get("summary") or "").strip() or None,
            tags=[str(tag).strip() for tag in (args.get("tags") or []) if str(tag).strip()],
            importance_score=0.6,
            metadata_json=metadata,
        )
        session.add(item)
        await session.flush()
        await self.memory_service.embed_items(
            session,
            [item],
            config={"memory": self.settings.memory.model_dump(mode="json")},
        )
        return {"ok": True, "memory_id": str(item.id)}

    async def _finalize_realtime_call(
        self,
        session: AsyncSession,
        *,
        call_record: CallRecord,
        user: User | None,
        persona: Persona | None,
        transcript: str,
        outcome: RealtimeSessionOutcome,
    ) -> None:
        call_record.transcript = transcript or call_record.transcript
        call_record.started_at = outcome.started_at or call_record.started_at or utc_now()
        call_record.ended_at = outcome.ended_at or utc_now()
        if call_record.started_at and call_record.ended_at:
            call_record.duration_seconds = max(
                int((call_record.ended_at - call_record.started_at).total_seconds()),
                0,
            )
        call_record.status = CallStatus.completed if transcript or outcome.end_reason else CallStatus.failed
        summary = await self._summarize_call(transcript=transcript, persona=persona)
        call_record.metadata_json = {
            **(call_record.metadata_json or {}),
            "summary": summary,
            "tool_events": outcome.tool_events,
            "session_events": outcome.session_events,
            "ended_by_tool": outcome.ended_by_tool,
            "end_reason": outcome.end_reason,
        }
        if transcript and user is not None:
            await self._extract_call_memories(
                session,
                user=user,
                persona=persona,
                call_record=call_record,
                transcript=transcript,
                summary=summary,
            )
        await session.flush()
        logger.info(
            "realtime_call_finalized",
            call_record_id=str(call_record.id),
            status=call_record.status.value,
            duration_seconds=call_record.duration_seconds,
            transcript_preview=(transcript or "")[:160],
            summary_preview=(summary or "")[:160],
        )

    async def _finalize_media_stream_call(
        self,
        session: AsyncSession,
        *,
        call_record: CallRecord,
        user: User | None,
        persona: Persona | None,
        outcome: MediaStreamSessionOutcome,
    ) -> None:
        call_record.transcript = outcome.transcript or call_record.transcript
        call_record.started_at = outcome.started_at or call_record.started_at or utc_now()
        call_record.ended_at = outcome.ended_at or utc_now()
        if call_record.started_at and call_record.ended_at:
            call_record.duration_seconds = max(
                int((call_record.ended_at - call_record.started_at).total_seconds()),
                0,
            )
        call_record.status = CallStatus.completed if outcome.transcript else CallStatus.failed
        summary = await self._summarize_call(transcript=outcome.transcript, persona=persona)
        call_record.metadata_json = {
            **(call_record.metadata_json or {}),
            "summary": summary,
            "session_events": outcome.session_events,
            "end_reason": outcome.end_reason,
        }
        if outcome.transcript and user is not None:
            await self._extract_call_memories(
                session,
                user=user,
                persona=persona,
                call_record=call_record,
                transcript=outcome.transcript,
                summary=summary,
            )
        await session.flush()
        logger.info(
            "media_stream_call_finalized",
            call_record_id=str(call_record.id),
            status=call_record.status.value,
            duration_seconds=call_record.duration_seconds,
            transcript_preview=(outcome.transcript or "")[:160],
            summary_preview=(summary or "")[:160],
        )

    async def _initial_greeting_text(
        self,
        *,
        call_record: CallRecord,
        user: User | None,
        persona: Persona | None,
        instructions: str,
    ) -> str:
        if call_record.direction == CallDirection.inbound:
            return f"hi {(user.display_name or '').strip()}".strip() if user and user.display_name else "hello?"
        prompt = self._initial_greeting_payload(call_record=call_record, user=user, persona=persona)["response"]["instructions"]
        response = await self.openai_provider.generate_text(
            instructions=instructions,
            input_items=[{"role": "user", "content": prompt}],
            model=self.settings.voice.text_model,
            max_output_tokens=80,
        )
        return response.text.strip()

    async def _summarize_call(self, *, transcript: str, persona: Persona | None) -> str:
        if not transcript:
            return ""
        if not self.openai_provider.enabled:
            return transcript[:280]
        response = await self.openai_provider.generate_text(
            instructions="Summarize this phone call for internal memory in 2-4 short sentences.",
            input_items=[
                {
                    "role": "user",
                    "content": (
                        f"Persona: {persona.display_name if persona else 'Companion'}\n"
                        f"Transcript:\n{transcript[:8000]}"
                    ),
                }
            ],
            max_output_tokens=220,
        )
        return response.text.strip()

    async def _extract_call_memories(
        self,
        session: AsyncSession,
        *,
        user: User,
        persona: Persona | None,
        call_record: CallRecord,
        transcript: str,
        summary: str,
    ) -> None:
        fake_message = Message(
            conversation_id=await self._conversation_id_for_call(session, user=user, persona=persona),
            user_id=user.id,
            persona_id=persona.id if persona else None,
            direction=Direction.inbound,
            channel=Channel.voice,
            provider="openai_realtime",
            idempotency_key=f"call-memory-{call_record.id}",
            body=summary or transcript[:1000],
            status=MessageStatus.received,
            metadata_json={"source": "call_summary", "call_record_id": str(call_record.id)},
        )
        session.add(fake_message)
        await session.flush()
        await self.memory_service.extract_from_message(
            session,
            user=user,
            persona=persona,
            message=fake_message,
            recent_messages=[fake_message],
            config={"memory": self.settings.memory.model_dump(mode="json")},
        )

    async def _conversation_id_for_call(
        self,
        session: AsyncSession,
        *,
        user: User,
        persona: Persona | None,
    ) -> Any:
        stmt = select(Conversation).where(Conversation.user_id == user.id)
        if persona is not None:
            stmt = stmt.where(Conversation.persona_id == persona.id)
        stmt = stmt.order_by(Conversation.updated_at.desc()).limit(1)
        conversation = (await session.execute(stmt)).scalar_one_or_none()
        if conversation is not None:
            return conversation.id
        conversation = Conversation(
            user_id=user.id,
            persona_id=persona.id if persona else None,
            status="open",
        )
        session.add(conversation)
        await session.flush()
        return conversation.id

    async def _resolve_call_record(self, session: AsyncSession, *, payload: dict[str, Any]) -> CallRecord | None:
        record_id = _nested_value(
            payload,
            "call_record_id",
            "data.call_record_id",
            "metadata.call_record_id",
            "call.metadata.call_record_id",
        )
        if record_id:
            record = await session.get(CallRecord, _maybe_uuid(record_id))
            if record is not None:
                return record
        provider_sid = _nested_value(payload, "twilio_call_sid", "data.twilio_call_sid", "metadata.twilio_call_sid")
        if provider_sid:
            stmt = select(CallRecord).where(CallRecord.provider_call_sid == provider_sid)
            record = (await session.execute(stmt)).scalar_one_or_none()
            if record is not None:
                return record
        return None

    async def _resolve_user_from_payload(self, session: AsyncSession, payload: dict[str, Any]) -> User | None:
        user_id = _nested_value(payload, "user_id", "data.user_id", "metadata.user_id", "call.metadata.user_id")
        if user_id:
            return await session.get(User, _maybe_uuid(user_id))
        phone = _nested_value(payload, "user_phone", "from", "data.from", "caller", "call.from")
        if not phone:
            return None
        target = _normalize_phone_number(str(phone))
        users = (await session.execute(select(User))).scalars().all()
        for user in users:
            if _normalize_phone_number(user.phone_number) == target:
                return user
        return None

    async def _resolve_persona_from_payload(
        self,
        session: AsyncSession,
        payload: dict[str, Any],
        *,
        user: User | None,
    ) -> Persona | None:
        persona_id = _nested_value(
            payload,
            "persona_id",
            "data.persona_id",
            "metadata.persona_id",
            "call.metadata.persona_id",
        )
        if persona_id:
            return await session.get(Persona, _maybe_uuid(persona_id))
        incoming_phone = _nested_value(payload, "user_phone", "from", "data.from", "caller", "call.from")
        normalized_incoming = _normalize_phone_number(str(incoming_phone)) if incoming_phone else ""
        if normalized_incoming:
            personas = (await session.execute(select(Persona))).scalars().all()
            for persona in personas:
                prompt_overrides = persona.prompt_overrides or {}
                raw_numbers = prompt_overrides.get("calling_numbers") or []
                if not isinstance(raw_numbers, list):
                    continue
                normalized_numbers = {_normalize_phone_number(str(number)) for number in raw_numbers if str(number).strip()}
                if normalized_incoming in normalized_numbers:
                    return persona
        if user and user.preferred_persona_id:
            return await session.get(Persona, user.preferred_persona_id)
        stmt = select(Persona).where(Persona.is_active.is_(True)).limit(1)
        return (await session.execute(stmt)).scalar_one_or_none()


def _extract_transcript_text(event: dict[str, Any]) -> str:
    if isinstance(event.get("delta"), str):
        return str(event["delta"])
    item = event.get("item") or {}
    if isinstance(item, dict):
        content = item.get("content")
        if isinstance(content, list):
            parts = []
            for block in content:
                if isinstance(block, dict):
                    text = block.get("text") or block.get("transcript") or block.get("content")
                    if text:
                        parts.append(str(text))
            return " ".join(parts).strip()
    response = event.get("response") or {}
    output = response.get("output") or []
    parts = []
    for output_item in output:
        if not isinstance(output_item, dict):
            continue
        for block in output_item.get("content", []):
            if isinstance(block, dict):
                text = block.get("text") or block.get("transcript")
                if text:
                    parts.append(str(text))
    return " ".join(parts).strip()


def _nested_value(payload: dict[str, Any], *paths: str) -> Any:
    for path in paths:
        cursor: Any = payload
        found = True
        for part in path.split("."):
            if isinstance(cursor, dict) and part in cursor:
                cursor = cursor[part]
            else:
                found = False
                break
        if found and cursor not in (None, ""):
            return cursor
    return None


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


def _mulaw_rms(chunk: bytes) -> int:
    if not chunk:
        return 0
    pcm = audioop.ulaw2lin(chunk, 2)
    return int(audioop.rms(pcm, 2))


def _mulaw_to_wav_bytes(chunk: bytes) -> bytes:
    pcm = audioop.ulaw2lin(chunk, 2)
    buffer = io.BytesIO()
    with wave.open(buffer, "wb") as wav_file:
        wav_file.setnchannels(1)
        wav_file.setsampwidth(2)
        wav_file.setframerate(8000)
        wav_file.writeframes(pcm)
    return buffer.getvalue()


def _chunk_audio_bytes(data: bytes, *, ms: int) -> list[bytes]:
    if not data:
        return []
    chunk_size = max(int((8000 * ms) / 1000), 160)
    return [data[index : index + chunk_size] for index in range(0, len(data), chunk_size)]


def _format_call_turn_prompt(transcript_entries: list[tuple[str, str]], latest_user_text: str) -> str:
    lines = ["Live phone call transcript so far:"]
    for speaker, text in transcript_entries[-12:]:
        lines.append(f"{speaker}: {text}")
    lines.append("")
    lines.append(f"Latest caller speech: {latest_user_text}")
    lines.append("Reply naturally in one to three short spoken sentences.")
    return "\n".join(lines)


def _flatten_transcript_entries(entries: list[tuple[str, str]]) -> str:
    return "\n".join(f"{speaker}: {text}".strip() for speaker, text in entries if text and text.strip()).strip()


def _event_value(payload: dict[str, Any], *keys: str) -> str:
    for key in keys:
        value = payload.get(key)
        if value:
            return str(value)
    return ""


def _parse_tool_args(event: dict[str, Any]) -> dict[str, Any]:
    candidates = [
        event.get("arguments"),
        _nested_value(event, "item.arguments"),
        _nested_value(event, "function.arguments"),
    ]
    for candidate in candidates:
        if isinstance(candidate, dict):
            return candidate
        if isinstance(candidate, str) and candidate.strip():
            with suppress(Exception):
                loaded = json.loads(candidate)
                if isinstance(loaded, dict):
                    return loaded
    return {}


def _coerce_call_status(status: str) -> CallStatus:
    normalized = (status or "").strip().lower()
    if normalized in {"queued", "initiated"}:
        return CallStatus.queued
    if normalized in {"ringing"}:
        return CallStatus.ringing
    if normalized in {"answered", "in-progress", "in_progress"}:
        return CallStatus.in_progress
    if normalized in {"completed", "finished"}:
        return CallStatus.completed
    if normalized in {"busy", "failed", "canceled", "cancelled"}:
        return CallStatus.failed
    if normalized in {"no-answer", "no_answer"}:
        return CallStatus.no_answer
    return CallStatus.failed


def _maybe_uuid(value: Any) -> Any:
    if isinstance(value, str):
        with suppress(ValueError):
            return uuid.UUID(value)
    return value
