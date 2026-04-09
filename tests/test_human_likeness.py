from __future__ import annotations

from app.models.communication import Conversation, Message
from app.models.enums import Channel, Direction, MessageStatus
from app.models.persona import Persona
from app.models.user import User
from app.services.conversation_state import ConversationStateService
from app.services.reply_ranker import ReplyRankerService
from app.services.safety import SafetyService
from app.services.alerting import AlertingService


class FakeClient:
    async def post(self, *args, **kwargs):
        class Response:
            status_code = 200

            def raise_for_status(self):
                return None

        return Response()


async def test_conversation_state_tracks_open_loop(sqlite_session):
    user = User(phone_number="+15555550120")
    persona = Persona(key="p1", display_name="Rowan", is_active=True)
    sqlite_session.add_all([user, persona])
    await sqlite_session.flush()
    conversation = Conversation(user_id=user.id, persona_id=persona.id)
    sqlite_session.add(conversation)
    await sqlite_session.flush()

    svc = ConversationStateService()
    state = await svc.get_or_create(sqlite_session, user=user, persona=persona, conversation=conversation)
    await svc.update_from_inbound(
        sqlite_session,
        state=state,
        inbound_text="do you remember my dentist appt tomorrow?",
        classification={"direct_question": True, "emotion": "neutral"},
        recent_messages=[],
    )

    assert state.open_loops
    assert state.open_loops[0]["status"] == "open"


def test_reply_ranker_prefers_novel_candidate():
    svc = ReplyRankerService()
    recent = [
        Message(direction=Direction.outbound, channel=Channel.sms, provider="sim", idempotency_key="a", status=MessageStatus.sent, body="i hear you, wanna talk more?"),
    ]
    ranked = svc.rank(
        candidates=["i hear you, wanna talk more?", "yeah that sounds hard, what happened next?"],
        inbound_text="can we talk",
        recent_messages=recent,
        classification={"direct_question": False},
    )
    assert ranked[0].text == "yeah that sounds hard, what happened next?"


async def test_safety_dependency_detector(sqlite_session, settings):
    service = SafetyService(AlertingService(settings, FakeClient()))
    result = service.check_outbound(
        text="if you leave me i will fall apart because youre mine",
        config=settings.model_dump(mode="json"),
    )
    assert result.blocked is True
    assert any("dependency" in reason for reason in result.reasons)
