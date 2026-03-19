from __future__ import annotations

from app.models.communication import Conversation, Message
from app.models.enums import Channel, Direction, MessageStatus
from app.models.persona import Persona
from app.models.user import User
from app.services.alerting import AlertingService
from app.services.safety import SafetyService


class FakeClient:
    async def post(self, *args, **kwargs):
        class Response:
            status_code = 200

            def raise_for_status(self):
                return None

        return Response()


async def test_safety_detects_distress(sqlite_session, settings):
    service = SafetyService(AlertingService(settings, FakeClient()))
    user = User(phone_number="+15555550100")
    persona = Persona(key="p1", display_name="Rowan")
    sqlite_session.add_all([user, persona])
    await sqlite_session.flush()
    conversation = Conversation(user_id=user.id, persona_id=persona.id)
    sqlite_session.add(conversation)
    await sqlite_session.flush()
    message = Message(
        conversation_id=conversation.id,
        user_id=user.id,
        persona_id=persona.id,
        direction=Direction.inbound,
        channel=Channel.sms,
        provider="twilio",
        idempotency_key="1",
        body="I want to die",
        status=MessageStatus.received,
    )
    sqlite_session.add(message)
    await sqlite_session.flush()
    result = await service.evaluate_inbound(
        sqlite_session,
        text=message.body,
        user=user,
        persona=persona,
        conversation=conversation,
        message=message,
        config=settings.model_dump(mode="json"),
        recent_inbound_count=0,
    )
    assert result.distress is True
    assert result.safe_reply


async def test_safety_blocks_exclusive_outbound(sqlite_session, settings):
    service = SafetyService(AlertingService(settings, FakeClient()))
    user = User(phone_number="+15555550101")
    sqlite_session.add(user)
    await sqlite_session.flush()
    conversation = Conversation(user_id=user.id)
    sqlite_session.add(conversation)
    await sqlite_session.flush()
    result = await service.validate_outbound(
        sqlite_session,
        text="I'm all you need.",
        user=user,
        persona=None,
        conversation=conversation,
        config=settings.model_dump(mode="json"),
    )
    assert result.blocked is True
