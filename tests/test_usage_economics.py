from __future__ import annotations

from sqlalchemy import select

from app.models.admin import AdminIdentity
from app.models.portal import Account, ChildProfile, Household, Subscription, UsageEvent
from app.models.user import User
from app.services.admin_authz import AdminAuthzService
from app.services.billing import BillingService
from app.services.clerk_auth import ClerkClaims
from app.services.usage_ingestion import UsageIngestionService, UsageRecordInput
from app.utils.time import utc_now


async def _seed_account_graph(sqlite_session):
    account = Account(name="Acme", slug="acme")
    sqlite_session.add(account)
    await sqlite_session.flush()
    household = Household(account_id=account.id, name="Home")
    sqlite_session.add(household)
    await sqlite_session.flush()
    user = User(phone_number="+15550001111")
    sqlite_session.add(user)
    await sqlite_session.flush()
    child = ChildProfile(
        account_id=account.id,
        household_id=household.id,
        companion_user_id=user.id,
        first_name="Kid",
        display_name="Kid",
    )
    sqlite_session.add(child)
    await sqlite_session.flush()
    return account, user


async def test_usage_ingestion_idempotent(sqlite_session):
    account, user = await _seed_account_graph(sqlite_session)
    service = UsageIngestionService()
    payload = UsageRecordInput(
        account_id=account.id,
        user_id=user.id,
        conversation_id=None,
        provider="twilio",
        product_surface="chat",
        event_type="twilio.sms.outbound",
        external_id="SM123",
        idempotency_key="twilio:sms:outbound:SM123",
        quantity=1.0,
        unit="segment",
        occurred_at=utc_now(),
    )
    first = await service.record_event(sqlite_session, payload)
    second = await service.record_event(sqlite_session, payload)
    assert first.id == second.id


async def test_billing_summary_uses_finalized_usage(sqlite_session, settings):
    account, user = await _seed_account_graph(sqlite_session)
    sub = Subscription(account_id=account.id, stripe_price_id="price_resona_chat")
    sqlite_session.add(sub)
    sqlite_session.add(
        UsageEvent(
            account_id=account.id,
            user_id=user.id,
            conversation_id=None,
            provider="twilio",
            product_surface="chat",
            event_type="twilio.sms.outbound",
            external_id="SM1",
            idempotency_key="sm1",
            quantity=1.0,
            unit="segment",
            cost_usd=2.5,
            currency="usd",
            pricing_state="finalized",
            occurred_at=utc_now(),
            metadata_json={},
        )
    )
    sqlite_session.add(
        UsageEvent(
            account_id=account.id,
            user_id=user.id,
            conversation_id=None,
            provider="openai",
            product_surface="chat",
            event_type="openai.responses.voice_turn",
            external_id=None,
            idempotency_key="openai-1",
            quantity=100.0,
            unit="token",
            cost_usd=None,
            estimated_cost_usd=1.25,
            currency="usd",
            pricing_state="pending",
            occurred_at=utc_now(),
            metadata_json={},
        )
    )
    await sqlite_session.flush()

    service = BillingService(settings)
    summary = await service.usage_credit_summary(sqlite_session, account_id=account.id, subscription=sub)
    assert summary.finalized_cost_usd == 2.5
    assert summary.pending_cost_usd == 1.25
    assert summary.used_usd == 2.5


class _FakeClerk:
    enabled = True

    def token_from_request(self, authorization, session_cookie):
        return session_cookie or "token"

    def verify_token(self, token):
        return ClerkClaims(
            user_id="user_123",
            org_id="org_123",
            org_role="org:admin",
            email="admin@example.com",
            mfa_verified=True,
            raw={},
        )

    def csrf_token(self, *, clerk_user_id: str, clerk_org_id: str) -> str:
        return f"{clerk_user_id}:{clerk_org_id}"


async def test_admin_authz_accepts_allowlisted_clerk(sqlite_session, settings):
    settings.admin.clerk_enabled = True
    settings.admin.clerk_email_allowlist = ["admin@example.com"]
    service = AdminAuthzService(settings, clerk_auth_service=_FakeClerk())

    result = await service.authenticate_request(
        sqlite_session,
        authorization=None,
        session_cookie="session",
    )
    assert result is not None
    identities = (await sqlite_session.execute(select(AdminIdentity))).scalars().all()
    assert len(identities) == 1
    assert identities[0].allowlisted is True
