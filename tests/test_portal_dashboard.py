from __future__ import annotations

from datetime import UTC, datetime

import httpx
from fastapi import FastAPI
from httpx import ASGITransport

from app.db.session import get_db_session
from app.models.communication import Conversation, Message, SafetyEvent
from app.models.enums import Channel, Direction, HouseholdRole, MessageStatus, SafetySeverity, SubscriptionStatus, MemoryType
from app.models.memory import MemoryItem
from app.models.persona import Persona
from app.models.portal import Account, ChildProfile, CustomerUser, Household, Subscription, UsageEvent
from app.models.user import User
from app.portal.dependencies import PortalRequestContext, require_portal_context
from app.routers import portal
from app.services.billing import BillingService


class _FakeClerkAuthService:
    enabled = True


class _FakeContainer:
    def __init__(self, settings) -> None:
        self.settings = settings
        self.clerk_auth_service = _FakeClerkAuthService()
        self.billing_service = BillingService(settings)


async def _dashboard_fixture(sqlite_session, settings, *, with_child: bool = True):
    account = Account(name="Resona Family", slug="resona-family", clerk_org_id="org_test")
    sqlite_session.add(account)
    await sqlite_session.flush()

    customer_user = CustomerUser(
        account_id=account.id,
        email="parent@example.com",
        password_hash="clerk:test",
        display_name="Parent",
        relationship_label="mom",
    )
    sqlite_session.add(customer_user)
    await sqlite_session.flush()

    household = Household(
        account_id=account.id,
        name="Resona Home",
        timezone="America/New_York",
    )
    sqlite_session.add(household)
    await sqlite_session.flush()

    child = None
    companion = None
    if with_child:
        companion = User(display_name="Katie", phone_number="+16105550111")
        sqlite_session.add(companion)
        await sqlite_session.flush()

        child = ChildProfile(
            account_id=account.id,
            household_id=household.id,
            first_name="Katie",
            display_name="Katie",
            companion_user_id=companion.id,
        )
        sqlite_session.add(child)

    await sqlite_session.commit()

    container = _FakeContainer(settings)
    context = PortalRequestContext(
        customer_user=customer_user,
        account_id=str(account.id),
        role=HouseholdRole.owner,
        clerk_user_id="user_test",
        clerk_org_id="org_test",
        mfa_verified=True,
        csrf_token="csrf-test",
        container=container,
    )

    app = FastAPI()
    app.include_router(portal.router)
    app.state.container = container

    async def _context_override():
        return context

    async def _session_override():
        return sqlite_session

    app.dependency_overrides[require_portal_context] = _context_override
    app.dependency_overrides[get_db_session] = _session_override
    return app, context, account, household, child, companion


async def test_dashboard_renders_usage_first_professional_sections(sqlite_session, settings):
    app, _, account, _, child, companion = await _dashboard_fixture(sqlite_session, settings)
    assert child is not None
    assert companion is not None

    conversation = Conversation(user_id=companion.id, status="open", metadata_json={})
    sqlite_session.add(conversation)
    await sqlite_session.flush()

    sqlite_session.add(
        Subscription(
            account_id=account.id,
            stripe_price_id="chat_monthly",
            status=SubscriptionStatus.active,
        )
    )
    sqlite_session.add_all(
        [
            UsageEvent(
                account_id=account.id,
                user_id=companion.id,
                provider="openai",
                product_surface="sms",
                event_type="completion",
                idempotency_key="usage-final-1",
                quantity=1.0,
                unit="request",
                cost_usd=3.25,
                pricing_state="finalized",
                estimated_cost_usd=3.25,
                occurred_at=datetime(2026, 4, 11, 14, 0, tzinfo=UTC),
                metadata_json={},
            ),
            UsageEvent(
                account_id=account.id,
                user_id=companion.id,
                provider="openai",
                product_surface="voice",
                event_type="transcription",
                idempotency_key="usage-pending-1",
                quantity=1.0,
                unit="minute",
                pricing_state="pending",
                estimated_cost_usd=1.10,
                occurred_at=datetime(2026, 4, 11, 14, 30, tzinfo=UTC),
                metadata_json={},
            ),
        ]
    )
    sqlite_session.add_all(
        [
            Message(
                conversation_id=conversation.id,
                user_id=companion.id,
                direction=Direction.inbound,
                channel=Channel.sms,
                provider="twilio",
                idempotency_key="message-1",
                body="Katie said she wants Taylor Swift on while she gets ready.",
                status=MessageStatus.received,
                created_at=datetime(2026, 4, 11, 13, 45, tzinfo=UTC),
                metadata_json={},
            ),
            Message(
                conversation_id=conversation.id,
                user_id=companion.id,
                direction=Direction.outbound,
                channel=Channel.sms,
                provider="twilio",
                idempotency_key="message-2",
                body="Resona suggested a gentle music-based morning routine.",
                status=MessageStatus.sent,
                created_at=datetime(2026, 4, 11, 13, 47, tzinfo=UTC),
                metadata_json={},
            ),
        ]
    )
    sqlite_session.add(
        MemoryItem(
            user_id=companion.id,
            memory_type=MemoryType.preference,
            title="Morning Music",
            content="Taylor Swift helps Katie settle into her morning routine.",
            summary="Taylor Swift helps Katie settle into her morning routine.",
            updated_at=datetime(2026, 4, 11, 13, 50, tzinfo=UTC),
        )
    )
    sqlite_session.add(
        SafetyEvent(
            user_id=companion.id,
            conversation_id=conversation.id,
            event_type="distress_signal",
            severity=SafetySeverity.high,
            detector="policy",
            action_taken="Flagged for parent review.",
            created_at=datetime(2026, 4, 11, 13, 55, tzinfo=UTC),
            details_json={},
        )
    )
    await sqlite_session.commit()

    async with httpx.AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        response = await client.get("/app/dashboard")

    assert response.status_code == 200
    assert "dashboard-usage-hero" in response.text
    assert "dashboard-status-strip" in response.text
    assert "dashboard-grid" in response.text
    assert response.text.index("Monthly Credit Usage") < response.text.index("Household Snapshot")
    assert "Included this month" in response.text
    assert "Used so far" in response.text
    assert "Still available" in response.text
    assert "Pending reconciliation" in response.text
    assert "$10.00" in response.text
    assert "$3.25" in response.text
    assert "$6.75" in response.text
    assert "Incoming" in response.text
    assert "SMS" in response.text
    assert "Morning Music" in response.text
    assert "Distress Signal" in response.text
    assert "Where to focus next" in response.text
    assert "Open Plans" in response.text
    assert "Open Questions" in response.text
    assert "/app/questions?question=" in response.text
    assert "Usage and Credits" not in response.text
    assert "Recent Conversation Transcript" not in response.text
    assert "Memory Highlights Used by Replies" not in response.text
    assert 'style="margin-top: 16px;' not in response.text


async def test_dashboard_empty_states_and_readiness_callout(sqlite_session, settings):
    app, _, _, _, _, _ = await _dashboard_fixture(sqlite_session, settings, with_child=False)

    async with httpx.AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        response = await client.get("/app/dashboard")

    assert response.status_code == 200
    assert "Portal access is ready whenever you are" in response.text
    assert "Review Setup" in response.text
    assert "Activate Plan" in response.text
    assert "No recent conversations yet" in response.text
    assert "No memory highlights yet" in response.text
    assert "No recent safety events" in response.text
    assert "No child linked" in response.text


async def test_guidance_pages_render_with_selected_child_context(sqlite_session, settings):
    app, _, _, _, child, companion = await _dashboard_fixture(sqlite_session, settings)
    assert child is not None
    assert companion is not None

    child.preferences_json = {
        "preferred_pacing": ["direct", "steady"],
        "response_style": ["encouraging"],
        "voice_enabled": True,
    }
    child.routines_json = {
        "daily_cadence": "adaptive",
    }
    sqlite_session.add(
        MemoryItem(
            user_id=companion.id,
            memory_type=MemoryType.preference,
            title="Favorite Music",
            content="Music helps her settle into the day.",
            summary="Music helps her settle into the day.",
            updated_at=datetime(2026, 4, 11, 13, 50, tzinfo=UTC),
        )
    )
    await sqlite_session.commit()

    async with httpx.AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        plans_response = await client.get("/app/plans")
        questions_response = await client.get("/app/questions")

    assert plans_response.status_code == 200
    assert "Support Plans" in plans_response.text
    assert "Refine in Parent Chat" in plans_response.text
    assert "Favorite Music" in plans_response.text

    assert questions_response.status_code == 200
    assert "Questions for Parents" in questions_response.text
    assert "Answer here" in questions_response.text
    assert "Talk it through with Resona" in questions_response.text
    assert "How hands-on do you want parent visibility to be?" in questions_response.text


async def test_questions_page_avoids_recent_memory_title_prompt_and_embeds_chat(sqlite_session, settings):
    app, _, _, _, child, companion = await _dashboard_fixture(sqlite_session, settings)
    assert child is not None
    assert companion is not None

    sqlite_session.add(
        MemoryItem(
            user_id=companion.id,
            memory_type=MemoryType.fact,
            title="Sabrina dinner for 2026-04-11",
            content="Sabrina had pasta for dinner and seemed especially relaxed.",
            summary="Sabrina had pasta for dinner.",
            updated_at=datetime(2026, 4, 11, 14, 5, tzinfo=UTC),
        )
    )
    await sqlite_session.commit()

    async with httpx.AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        response = await client.get("/app/questions")

    assert response.status_code == 200
    assert "What detail would make the Sabrina dinner for 2026-04-11 picture more useful?" not in response.text
    assert "Answer what would help most right now" in response.text
    assert 'id="parent-chat-form"' in response.text
    assert 'id="parent-chat-thread"' in response.text
    assert 'id="parent-chat-context"' in response.text


async def test_questions_page_uses_active_persona_name_and_respects_question_query(sqlite_session, settings):
    app, _, _, _, child, companion = await _dashboard_fixture(sqlite_session, settings)
    assert child is not None
    assert companion is not None

    persona = Persona(
        key="juniper",
        display_name="Juniper",
        source_type="portal_preset",
    )
    sqlite_session.add(persona)
    await sqlite_session.flush()
    companion.preferred_persona_id = persona.id
    await sqlite_session.commit()

    async with httpx.AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        response = await client.get("/app/questions?question=what-backfires")

    assert response.status_code == 200
    assert "Talk it through with Juniper" in response.text
    assert "ongoing Juniper conversation" in response.text
    assert 'data-selected-question-key="what-backfires"' in response.text
