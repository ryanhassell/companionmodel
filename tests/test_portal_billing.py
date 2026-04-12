from __future__ import annotations

from datetime import UTC, datetime

import httpx
from fastapi import FastAPI
from httpx import ASGITransport

from app.db.session import get_db_session
from app.models.enums import HouseholdRole, SubscriptionStatus
from app.models.portal import Account, ChildProfile, CustomerUser, Household, Subscription, UsageEvent
from app.portal.dependencies import PortalRequestContext, require_owner_mfa_context, require_portal_context
from app.routers import portal
from app.services.billing import BillingService
from app.services.portal_initialization import PortalInitializationService


class _FakeClerkAuthService:
    enabled = True


class _FakeContainer:
    def __init__(self, settings) -> None:
        self.settings = settings
        self.clerk_auth_service = _FakeClerkAuthService()
        self.billing_service = BillingService(settings)
        self.portal_initialization_service = PortalInitializationService(settings, self.billing_service)


class _FakeStripeBillingPortalSession:
    created_kwargs = None

    @classmethod
    def create(cls, **kwargs):
        cls.created_kwargs = kwargs
        return {"id": "bps_123", "url": "https://billing.stripe.test/session"}


class _FakeStripeBillingPortal:
    Session = _FakeStripeBillingPortalSession


class _FakeStripe:
    billing_portal = _FakeStripeBillingPortal()


async def test_billing_page_renders_usage_first_household_summary(sqlite_session, settings):
    account = Account(name="Resona Family", slug="resona-family", clerk_org_id="org_test")
    sqlite_session.add(account)
    await sqlite_session.flush()

    customer_user = CustomerUser(
        account_id=account.id,
        email="parent@example.com",
        password_hash="clerk:test",
        display_name="Parent",
    )
    sqlite_session.add(customer_user)
    await sqlite_session.flush()

    household = Household(account_id=account.id, name="Resona Home", timezone="America/New_York")
    sqlite_session.add(household)
    await sqlite_session.flush()

    sqlite_session.add_all(
        [
            ChildProfile(account_id=account.id, household_id=household.id, first_name="Katie", display_name="Katie", birth_year=2004),
            ChildProfile(account_id=account.id, household_id=household.id, first_name="Sophie", display_name="Sophie", birth_year=2010),
        ]
    )
    sqlite_session.add(
        Subscription(
            account_id=account.id,
            stripe_price_id="chat_monthly",
            status=SubscriptionStatus.active,
        )
    )
    sqlite_session.add(
        UsageEvent(
            account_id=account.id,
            user_id=None,
            provider="openai",
            product_surface="sms",
            event_type="completion",
            idempotency_key="billing-usage-1",
            quantity=1.0,
            unit="request",
            cost_usd=2.5,
            estimated_cost_usd=2.5,
            pricing_state="finalized",
            occurred_at=datetime(2026, 4, 11, 14, 0, tzinfo=UTC),
            metadata_json={},
        )
    )
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

    async with httpx.AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        response = await client.get("/app/billing")

    assert response.status_code == 200
    assert "Monthly Credit Usage" in response.text
    assert response.text.index("Monthly Credit Usage") < response.text.index("Plan overview")
    assert "Profiles on this account" in response.text
    assert "Additional child add-on" in response.text
    assert "Plan one household with room to grow" in response.text
    assert "Manage Profiles" in response.text
    assert "$10.00" in response.text
    assert "$2.50" in response.text


async def test_billing_manage_route_redirects_to_stripe_portal(sqlite_session, settings):
    settings.stripe.enabled = True
    account = Account(name="Resona Family", slug="resona-family", clerk_org_id="org_test")
    sqlite_session.add(account)
    await sqlite_session.flush()

    customer_user = CustomerUser(
        account_id=account.id,
        email="parent@example.com",
        password_hash="clerk:test",
        display_name="Parent",
    )
    sqlite_session.add(customer_user)
    await sqlite_session.flush()

    subscription = Subscription(
        account_id=account.id,
        stripe_price_id="chat_monthly",
        stripe_customer_id="cus_123",
        status=SubscriptionStatus.active,
    )
    sqlite_session.add(subscription)
    await sqlite_session.commit()

    container = _FakeContainer(settings)
    container.billing_service._stripe = _FakeStripe()
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
    app.dependency_overrides[require_owner_mfa_context] = _context_override
    app.dependency_overrides[get_db_session] = _session_override

    async with httpx.AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        response = await client.post(
            "/app/billing/manage",
            data={"csrf_token": "csrf-test"},
            follow_redirects=False,
        )

    assert response.status_code == 303
    assert response.headers["location"] == "https://billing.stripe.test/session"
    assert _FakeStripeBillingPortalSession.created_kwargs["customer"] == "cus_123"
