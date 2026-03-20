from __future__ import annotations

import json
from contextlib import asynccontextmanager

import httpx
from fastapi import FastAPI
from httpx import ASGITransport
from sqlalchemy import select

from app.admin.dependencies import get_container, require_admin_context
from app.db.session import get_db_session
from app.models.communication import CallRecord
from app.models.enums import CallDirection, CallStatus, MemoryType
from app.models.memory import MemoryItem
from app.models.persona import Persona
from app.models.user import User
from app.providers.base import OutboundCallResult
from app.providers.elevenlabs import ElevenLabsProvider
from app.providers.openai import OpenAIProvider
from app.providers.twilio import TwilioProvider
from app.routers.api import router as api_router
from app.services.daily_life import DailyLifeService
from app.services.memory import MemoryService
from app.services.prompt import PromptService
from app.services.voice import VoiceService


class FakeTwilioProvider:
    async def initiate_call(self, **kwargs):
        return OutboundCallResult(provider_sid="CA123", status="queued", raw_response={"sid": "CA123", **kwargs})


class FakeWebsocket:
    def __init__(self, events: list[dict[str, object]]) -> None:
        self.events = [json.dumps(event) for event in events]
        self.sent: list[dict[str, object]] = []

    async def send(self, payload: str) -> None:
        self.sent.append(json.loads(payload))

    async def recv(self) -> str:
        if not self.events:
            return json.dumps({"type": "call.ended"})
        return self.events.pop(0)


def _build_voice_service(settings) -> tuple[VoiceService, OpenAIProvider]:
    client = httpx.AsyncClient()
    openai_provider = OpenAIProvider(settings, client)
    elevenlabs_provider = ElevenLabsProvider(settings, client)
    prompt_service = PromptService(settings)
    memory_service = MemoryService(settings, openai_provider, prompt_service)
    daily_life_service = DailyLifeService(memory_service)
    service = VoiceService(
        settings,
        FakeTwilioProvider(),  # type: ignore[arg-type]
        openai_provider,
        elevenlabs_provider,
        prompt_service,
        memory_service,
        daily_life_service,
    )
    return service, openai_provider


async def test_voice_service_initiates_realtime_call(sqlite_session, settings):
    settings.voice.enabled = True
    settings.voice.driver = "openai_realtime_sip"
    settings.voice.realtime_sip_uri = "sip:test@example.com"
    user = User(phone_number="+15555550110", timezone="America/New_York")
    persona = Persona(key="sabrina", display_name="Sabrina", is_active=True)
    sqlite_session.add_all([user, persona])
    await sqlite_session.flush()
    service, openai_provider = _build_voice_service(settings)

    record = await service.initiate_realtime_call(
        sqlite_session,
        user=user,
        persona=persona,
        config={"voice": {}, "safety": {}},
        opening_line="hey there",
    )

    assert record.status == CallStatus.queued
    assert record.direction == CallDirection.outbound
    assert record.provider_call_sid == "CA123"
    assert record.metadata_json["transport"] == "twilio_pstn_openai_sip"
    assert "x-record-id=" in record.metadata_json["twilio"]["twiml"]
    await openai_provider.client.aclose()


async def test_voice_service_accepts_incoming_realtime_call(sqlite_session, settings, monkeypatch):
    settings.voice.enabled = True
    settings.voice.driver = "openai_realtime_sip"
    user = User(phone_number="+15555550111", timezone="America/New_York")
    persona = Persona(key="sabrina", display_name="Sabrina", is_active=True)
    sqlite_session.add_all([user, persona])
    await sqlite_session.flush()
    record = CallRecord(
        user_id=user.id,
        persona_id=persona.id,
        direction=CallDirection.outbound,
        status=CallStatus.queued,
        to_number=user.phone_number,
        from_number="+15550000000",
        metadata_json={"mode": "realtime"},
    )
    sqlite_session.add(record)
    await sqlite_session.flush()

    service, openai_provider = _build_voice_service(settings)
    scheduled: list[tuple[str, str]] = []
    accepted_payload: dict[str, object] = {}

    async def fake_accept(call_id: str, *, payload: dict[str, object]):
        accepted_payload.update(payload)
        return {"id": call_id, "status": "accepted", "payload": payload}

    monkeypatch.setattr(openai_provider, "accept_realtime_call", fake_accept)
    monkeypatch.setattr(service, "_schedule_sideband_session", lambda call_record_id, call_id: scheduled.append((call_record_id, call_id)))

    result = await service.handle_openai_realtime_event(
        sqlite_session,
        payload={
            "type": "realtime.call.incoming",
            "call_id": "call_123",
            "metadata": {"call_record_id": str(record.id)},
        },
    )

    assert result["status"] == "accepted"
    assert scheduled == [(str(record.id), "call_123")]
    assert record.provider_call_sid == "call_123"
    assert record.status == CallStatus.ringing
    assert accepted_payload["type"] == "realtime"
    assert accepted_payload["model"] == settings.openai.realtime_model
    assert accepted_payload["audio"]["output"]["voice"] == settings.voice.realtime_voice
    assert "turn_detection" not in accepted_payload
    await openai_provider.client.aclose()


async def test_voice_service_routes_incoming_call_to_persona_by_calling_number(sqlite_session, settings, monkeypatch):
    settings.voice.enabled = True
    settings.voice.driver = "openai_realtime_sip"
    user = User(phone_number="+15555550114", timezone="America/New_York")
    persona_a = Persona(key="sabrina", display_name="Sabrina", is_active=True)
    persona_b = Persona(
        key="ella",
        display_name="Ella",
        is_active=False,
        prompt_overrides={"calling_numbers": ["+1 (555) 555-0114"]},
    )
    sqlite_session.add_all([user, persona_a, persona_b])
    await sqlite_session.flush()

    service, openai_provider = _build_voice_service(settings)
    accepted_payload: dict[str, object] = {}

    async def fake_accept(call_id: str, *, payload: dict[str, object]):
        accepted_payload.update(payload)
        return {"id": call_id, "status": "accepted", "payload": payload}

    monkeypatch.setattr(openai_provider, "accept_realtime_call", fake_accept)
    monkeypatch.setattr(service, "_schedule_sideband_session", lambda call_record_id, call_id: None)

    result = await service.handle_openai_realtime_event(
        sqlite_session,
        payload={
            "type": "realtime.call.incoming",
            "call_id": "call_route_1",
            "from": "+15555550114",
            "to": "+15550000000",
        },
    )

    assert result["status"] == "accepted"
    record = (await sqlite_session.execute(select(CallRecord).where(CallRecord.provider_call_sid == "call_route_1"))).scalar_one()
    assert str(record.persona_id) == str(persona_b.id)
    assert accepted_payload["audio"]["output"]["voice"] == settings.voice.realtime_voice
    await openai_provider.client.aclose()


async def test_voice_service_rejects_unknown_incoming_caller(sqlite_session, settings, monkeypatch):
    settings.voice.enabled = True
    settings.voice.driver = "openai_realtime_sip"
    persona = Persona(key="sabrina", display_name="Sabrina", is_active=True)
    sqlite_session.add(persona)
    await sqlite_session.flush()

    service, openai_provider = _build_voice_service(settings)
    ended: list[str] = []

    async def fake_end(call_id: str):
        ended.append(call_id)
        return {"id": call_id, "status": "ended"}

    monkeypatch.setattr(openai_provider, "end_realtime_call", fake_end)

    result = await service.handle_openai_realtime_event(
        sqlite_session,
        payload={
            "type": "realtime.call.incoming",
            "call_id": "call_unknown_1",
            "from": "+15555550999",
            "to": "+15550000000",
        },
    )

    assert result["status"] == "rejected"
    assert result["reason"] == "unknown_caller"
    assert ended == ["call_unknown_1"]
    records = list((await sqlite_session.execute(select(CallRecord))).scalars().all())
    assert records == []
    await openai_provider.client.aclose()


async def test_voice_service_stream_and_finalize_realtime_call(sqlite_session, settings, monkeypatch):
    settings.voice.enabled = True
    settings.voice.driver = "openai_realtime_sip"
    user = User(phone_number="+15555550112", timezone="America/New_York")
    persona = Persona(key="sabrina", display_name="Sabrina", is_active=True)
    sqlite_session.add_all([user, persona])
    await sqlite_session.flush()
    record = CallRecord(
        user_id=user.id,
        persona_id=persona.id,
        direction=CallDirection.inbound,
        status=CallStatus.in_progress,
        from_number=user.phone_number,
        to_number="+15550000000",
        provider_call_sid="call_456",
        metadata_json={"mode": "realtime", "transport": "openai_sip_inbound"},
    )
    sqlite_session.add(record)
    await sqlite_session.flush()

    service, openai_provider = _build_voice_service(settings)
    fake_ws = FakeWebsocket(
        [
            {"type": "session.started"},
            {"type": "conversation.item.created", "item": {"content": [{"text": "hey I saw Joe today"}]}},
            {
                "type": "response.function_call_arguments.done",
                "name": "save_call_memory",
                "call_id": "tool_1",
                "arguments": json.dumps(
                    {
                        "title": "Saw Joe",
                        "content": "The user saw Joe today.",
                        "summary": "User saw Joe today.",
                        "tags": ["joe", "plans"],
                        "memory_type": "episode",
                        "entity_name": "Joe",
                        "entity_kind": "person",
                    }
                ),
            },
            {
                "type": "response.function_call_arguments.done",
                "name": "end_call",
                "call_id": "tool_2",
                "arguments": json.dumps({"reason": "wrapped up naturally"}),
            },
        ]
    )

    @asynccontextmanager
    async def fake_sideband(call_id: str):
        assert call_id == "call_456"
        yield fake_ws

    monkeypatch.setattr(openai_provider, "open_realtime_sideband", fake_sideband)
    monkeypatch.setattr(service, "_summarize_call", lambda transcript, persona: _async_return("Quick summary about Joe."))

    outcome = await service._stream_realtime_session(
        sqlite_session,
        call_record=record,
        user=user,
        persona=persona,
        config={
            "voice": settings.voice.model_dump(mode="json"),
            "memory": settings.memory.model_dump(mode="json"),
            "app": {"timezone": user.timezone},
        },
        call_id="call_456",
    )
    await service._finalize_realtime_call(
        sqlite_session,
        call_record=record,
        user=user,
        persona=persona,
        transcript=outcome.transcript,
        outcome=outcome,
    )

    memories = list((await sqlite_session.execute(select(MemoryItem))).scalars().all())
    assert "hey I saw Joe today" in record.transcript
    assert record.status == CallStatus.completed
    assert record.metadata_json["summary"] == "Quick summary about Joe."
    assert any(item.metadata_json.get("source") in {"call_tool", "entity_merge"} for item in memories)
    session_update = fake_ws.sent[0]
    assert session_update["type"] == "session.update"
    assert session_update["session"]["output_modalities"] == ["audio"]
    assert session_update["session"]["audio"]["output"]["voice"] == settings.voice.realtime_voice
    assert session_update["session"]["audio"]["input"]["turn_detection"]["type"] == "semantic_vad"
    assert session_update["session"]["audio"]["input"]["turn_detection"]["eagerness"] == "low"
    assert session_update["session"]["audio"]["input"]["turn_detection"]["interrupt_response"] is False
    greeting = fake_ws.sent[1]
    assert greeting["type"] == "response.create"
    assert "Stay fully in character for this entire call." in greeting["response"]["instructions"]
    assert 'Say exactly this first spoken line and nothing else yet: "hello?".' in greeting["response"]["instructions"]
    assert "Do not add a follow-up question." in greeting["response"]["instructions"]

    await openai_provider.client.aclose()


async def test_calls_initiate_api_returns_transport(sqlite_session, settings):
    settings.voice.enabled = True
    settings.voice.driver = "openai_realtime_sip"
    user = User(phone_number="+15555550113", timezone="America/New_York")
    persona = Persona(key="sabrina", display_name="Sabrina", is_active=True)
    sqlite_session.add_all([user, persona])
    await sqlite_session.commit()

    class FakeConversationService:
        async def get_active_persona(self, session, user):
            return persona

    class FakeConfigService:
        async def get_effective_config(self, session, *, user=None, persona=None):
            return {"voice": {}, "safety": {}}

    class FakeVoiceService:
        async def initiate_call(self, session, *, user, persona, config, opening_line=None):
            return CallRecord(
                id="00000000-0000-0000-0000-000000000123",
                user_id=user.id,
                persona_id=persona.id,
                direction=CallDirection.outbound,
                status=CallStatus.queued,
                metadata_json={"transport": "twilio_pstn_openai_sip"},
            )

    class FakeContainer:
        conversation_service = FakeConversationService()
        config_service = FakeConfigService()
        voice_service = FakeVoiceService()

    app = FastAPI()
    app.include_router(api_router)

    async def override_session():
        yield sqlite_session

    async def override_container():
        return FakeContainer()

    async def override_admin():
        return object()

    app.dependency_overrides[get_db_session] = override_session
    app.dependency_overrides[get_container] = override_container
    app.dependency_overrides[require_admin_context] = override_admin

    async with httpx.AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        response = await client.post(
            "/api/calls/initiate",
            json={"user_id": str(user.id), "persona_id": str(persona.id), "opening_line": "hey"},
        )

    payload = response.json()
    assert response.status_code == 200
    assert payload["transport"] == "twilio_pstn_openai_sip"
    assert payload["status"] == "queued"


async def test_voice_service_initiates_media_stream_call(sqlite_session, settings):
    settings.voice.enabled = True
    settings.voice.driver = "twilio_media_streams_openai_stt_elevenlabs"
    settings.voice.elevenlabs_default_voice_id = "voice_123"
    user = User(phone_number="+15555550115", timezone="America/New_York")
    persona = Persona(key="sabrina", display_name="Sabrina", is_active=True)
    sqlite_session.add_all([user, persona])
    await sqlite_session.flush()
    service, openai_provider = _build_voice_service(settings)

    record = await service.initiate_call(
        sqlite_session,
        user=user,
        persona=persona,
        config={"voice": {}, "safety": {}},
        opening_line="hey there",
    )

    assert record.status == CallStatus.queued
    assert record.metadata_json["transport"] == "twilio_media_streams"
    twiml = record.metadata_json["twilio"].get("Twiml") or record.metadata_json["twilio"].get("twiml") or ""
    assert "<Connect><Stream" in twiml
    assert "call_record_id" in twiml
    await openai_provider.client.aclose()


async def test_voice_service_handles_inbound_twilio_voice_webhook_for_media_streams(sqlite_session, settings):
    settings.voice.enabled = True
    settings.voice.driver = "twilio_media_streams_openai_stt_elevenlabs"
    settings.voice.elevenlabs_default_voice_id = "voice_123"
    user = User(phone_number="+15555550116", timezone="America/New_York")
    persona = Persona(
        key="sabrina",
        display_name="Sabrina",
        is_active=True,
        prompt_overrides={"calling_numbers": ["+15555550116"], "elevenlabs_voice_id": "voice_abc"},
    )
    sqlite_session.add_all([user, persona])
    await sqlite_session.flush()
    service, openai_provider = _build_voice_service(settings)

    twiml = await service.handle_twilio_voice_webhook(
        sqlite_session,
        form={"From": "+15555550116", "To": "+15550000000", "CallSid": "CA999"},
    )

    assert "<Connect><Stream" in twiml
    record = (await sqlite_session.execute(select(CallRecord).where(CallRecord.provider_call_sid == "CA999"))).scalar_one()
    assert str(record.persona_id) == str(persona.id)
    await openai_provider.client.aclose()


async def _async_return(value):
    return value
