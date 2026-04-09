from __future__ import annotations

from app.models.communication import Conversation, Message
from app.models.enums import Channel, Direction, MessageStatus
from app.models.persona import Persona
from app.models.user import User
from app.services.candidate_reply import CandidateReplyService
from app.services.human_likeness import HumanLikenessService
from app.services.prompt import PromptService
from app.services.reply_ranker import ReplyRankerService
from app.services.turn_classifier import TurnClassifierService


class FakeOpenAIProvider:
    enabled = False


async def test_scoreboard_metrics_computes_values(sqlite_session, settings):
    user = User(phone_number="+15555550200")
    persona = Persona(key="p1", display_name="Rowan", is_active=True)
    sqlite_session.add_all([user, persona])
    await sqlite_session.flush()
    conversation = Conversation(user_id=user.id, persona_id=persona.id)
    sqlite_session.add(conversation)
    await sqlite_session.flush()

    rows = [
        Message(
            conversation_id=conversation.id,
            user_id=user.id,
            persona_id=persona.id,
            direction=Direction.inbound,
            channel=Channel.sms,
            provider="sim",
            idempotency_key="1",
            body="how are you?",
            status=MessageStatus.received,
        ),
        Message(
            conversation_id=conversation.id,
            user_id=user.id,
            persona_id=persona.id,
            direction=Direction.outbound,
            channel=Channel.sms,
            provider="sim",
            idempotency_key="2",
            body="i'm doing okay, thanks for asking",
            status=MessageStatus.sent,
        ),
    ]
    sqlite_session.add_all(rows)
    await sqlite_session.flush()

    service = HumanLikenessService(
        TurnClassifierService(FakeOpenAIProvider(), PromptService(settings)),
        CandidateReplyService(FakeOpenAIProvider(), PromptService(settings)),
        ReplyRankerService(),
    )
    metrics = await service.scoreboard_metrics(sqlite_session, user=user, lookback=20)

    assert metrics["outbound_count"] == 1
    assert 0.0 <= metrics["score"] <= 1.0


async def test_ab_replay_runs_without_model(sqlite_session, settings):
    user = User(phone_number="+15555550201")
    persona = Persona(key="p2", display_name="Rowan", is_active=True)
    sqlite_session.add_all([user, persona])
    await sqlite_session.flush()
    conversation = Conversation(user_id=user.id, persona_id=persona.id)
    sqlite_session.add(conversation)
    await sqlite_session.flush()

    sqlite_session.add_all(
        [
            Message(
                conversation_id=conversation.id,
                user_id=user.id,
                persona_id=persona.id,
                direction=Direction.inbound,
                channel=Channel.sms,
                provider="sim",
                idempotency_key="a",
                body="what should i eat",
                status=MessageStatus.received,
            ),
            Message(
                conversation_id=conversation.id,
                user_id=user.id,
                persona_id=persona.id,
                direction=Direction.outbound,
                channel=Channel.sms,
                provider="sim",
                idempotency_key="b",
                body="maybe a sandwich",
                status=MessageStatus.sent,
            ),
        ]
    )
    await sqlite_session.flush()

    service = HumanLikenessService(
        TurnClassifierService(FakeOpenAIProvider(), PromptService(settings)),
        CandidateReplyService(FakeOpenAIProvider(), PromptService(settings)),
        ReplyRankerService(),
    )
    replay = await service.run_ab_replay(
        sqlite_session,
        user=user,
        persona=persona,
        config=settings.model_dump(mode="json"),
        max_turns=10,
    )
    assert replay["turns"] == 1
    assert "summary" in replay


async def test_daily_score_series_has_expected_window(sqlite_session, settings):
    user = User(phone_number="+15555550202")
    persona = Persona(key="p3", display_name="Rowan", is_active=True)
    sqlite_session.add_all([user, persona])
    await sqlite_session.flush()
    conversation = Conversation(user_id=user.id, persona_id=persona.id)
    sqlite_session.add(conversation)
    await sqlite_session.flush()

    sqlite_session.add(
        Message(
            conversation_id=conversation.id,
            user_id=user.id,
            persona_id=persona.id,
            direction=Direction.outbound,
            channel=Channel.sms,
            provider="sim",
            idempotency_key="c",
            body="just checking in",
            status=MessageStatus.sent,
        )
    )
    await sqlite_session.flush()

    service = HumanLikenessService(
        TurnClassifierService(FakeOpenAIProvider(), PromptService(settings)),
        CandidateReplyService(FakeOpenAIProvider(), PromptService(settings)),
        ReplyRankerService(),
    )
    series = await service.daily_score_series(sqlite_session, user=user, days=7)
    assert len(series) == 7
    assert all("date" in point and "score" in point for point in series)
