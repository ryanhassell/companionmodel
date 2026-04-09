from __future__ import annotations

import httpx
from fastapi import FastAPI
from httpx import ASGITransport
from sqlalchemy import select

from app.db.session import get_db_session
from app.models.portal import Account, ChildProfile, CustomerUser, Household, PortalSession
from app.models.enums import HouseholdRole
from app.routers import portal
from app.core.security import stable_token_hash
from app.services.clerk_auth import ClerkClaims, TenantContext
from app.services.customer_auth import CustomerAuthService


async def test_customer_registration_creates_account_graph(sqlite_session, settings):
    service = CustomerAuthService(settings)

    user, email_token, otp = await service.register_user(
        sqlite_session,
        email="Parent@example.com",
        password="super-secure-password",
        display_name="Parent One",
        phone_number="+16105550123",
        accepted_terms=True,
        accepted_privacy=True,
        ip_address="127.0.0.1",
        user_agent="pytest",
    )
    await sqlite_session.commit()

    assert user.email == "parent@example.com"
    assert email_token
    assert otp


async def test_portal_session_roundtrip(sqlite_session, settings):
    service = CustomerAuthService(settings)
    user, _, _ = await service.register_user(
        sqlite_session,
        email="login@example.com",
        password="another-secure-password",
        display_name="Login User",
        phone_number=None,
        accepted_terms=True,
        accepted_privacy=True,
        ip_address="127.0.0.1",
        user_agent="pytest",
    )
    token, _ = await service.create_portal_session(
        sqlite_session,
        customer_user=user,
        user_agent="pytest",
        ip_address="127.0.0.1",
        trusted_device=True,
    )
    await sqlite_session.commit()

    resolved = await service.resolve_portal_session(sqlite_session, raw_token=token)
    assert resolved is not None
    assert resolved.customer_user.id == user.id


async def test_clerk_auth_sync_sets_durable_portal_cookies(sqlite_session, settings):
    settings.clerk.enabled = True
    settings.clerk.publishable_key = "pk_test"
    settings.clerk.issuer = "https://example.clerk.accounts.dev"
    settings.clerk.audience = None
    settings.customer_portal.secure_cookies = False

    account = Account(name="Resona Family", slug="resona-family", clerk_org_id="org_test")
    sqlite_session.add(account)
    await sqlite_session.flush()

    user = CustomerUser(
        account_id=account.id,
        email="parent@example.com",
        password_hash="clerk:test",
        display_name="Parent",
        relationship_label="mom",
        clerk_user_id="user_test",
        verification_level="verified",
    )
    sqlite_session.add(user)
    await sqlite_session.commit()

    tenant = TenantContext(
        account=account,
        customer_user=user,
        role=HouseholdRole.owner,
        clerk_user_id="user_test",
        clerk_org_id="org_test",
        mfa_verified=True,
    )

    class _FakeClerkAuthService:
        enabled = True

        def verify_token(self, token: str):
            return ClerkClaims(
                user_id="user_test",
                org_id="org_test",
                org_role="org:admin",
                email="parent@example.com",
                mfa_verified=True,
                raw={},
            )

        async def resolve_tenant_context(self, session, claims):
            return tenant

        def create_portal_session_token(self, tenant_context):
            return "signed-clerk-context"

    class _FakeContainer:
        def __init__(self) -> None:
            self.settings = settings
            self.clerk_auth_service = _FakeClerkAuthService()
            self.customer_auth_service = CustomerAuthService(settings)

    app = FastAPI()
    app.include_router(portal.router)
    app.state.container = _FakeContainer()

    async def _session_override():
        return sqlite_session

    app.dependency_overrides[get_db_session] = _session_override

    async with httpx.AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        response = await client.post("/app/auth/sync", json={"token": "clerk-jwt"})

    assert response.status_code == 200
    cookies = response.headers.get_list("set-cookie")
    assert any(settings.clerk.backend_session_cookie_name in value for value in cookies)
    assert any(settings.customer_portal.session_cookie_name in value for value in cookies)

    portal_cookie = next(value for value in cookies if settings.customer_portal.session_cookie_name in value)
    raw_token = portal_cookie.split(f"{settings.customer_portal.session_cookie_name}=", 1)[1].split(";", 1)[0]
    stored_session = await sqlite_session.scalar(
        select(PortalSession).where(PortalSession.session_token_hash == stable_token_hash(raw_token, settings))
    )
    assert stored_session is not None
    assert stored_session.customer_user_id == user.id


async def test_onboarding_creates_household_and_child(sqlite_session, settings):
    service = CustomerAuthService(settings)
    user, _, _ = await service.register_user(
        sqlite_session,
        email="guardian@example.com",
        password="guardian-secure-password",
        display_name="Guardian",
        phone_number=None,
        accepted_terms=True,
        accepted_privacy=True,
        ip_address="127.0.0.1",
        user_agent="pytest",
    )

    household, child = await service.complete_onboarding(
        sqlite_session,
        customer_user=user,
        mode="for_someone_else",
        relationship="guardian",
        household_name="Maple Home",
        child_name="Katie",
        timezone="America/New_York",
        child_phone_number=None,
    )
    await sqlite_session.commit()

    assert household.name == "Maple Home"
    assert child.display_name == "Katie"

    loaded_household = await sqlite_session.get(Household, household.id)
    loaded_child = await sqlite_session.get(ChildProfile, child.id)
    loaded_user = await sqlite_session.get(CustomerUser, user.id)
    assert loaded_household is not None
    assert loaded_child is not None
    assert loaded_user is not None
    assert loaded_user.relationship_label == "guardian"
