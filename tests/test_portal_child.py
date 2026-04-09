from __future__ import annotations

import uuid

import httpx
from fastapi import FastAPI
from httpx import ASGITransport

from app.db.session import get_db_session
from app.models.enums import HouseholdRole
from app.models.portal import Account, ChildProfile, CustomerUser, Household
from app.portal.dependencies import PortalRequestContext, require_portal_context
from app.routers import portal


class _FakeClerkAuthService:
    enabled = True


class _FakeContainer:
    def __init__(self, settings) -> None:
        self.settings = settings
        self.clerk_auth_service = _FakeClerkAuthService()


async def test_child_profile_page_renders_human_readable_sections(settings, sqlite_session):
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

    household = Household(
        account_id=account.id,
        name="Resona Home",
        timezone="America/New_York",
    )
    sqlite_session.add(household)
    await sqlite_session.flush()

    child = ChildProfile(
        account_id=account.id,
        household_id=household.id,
        first_name="Katie",
        display_name="Katie",
        birth_year=2004,
        companion_user_id=uuid.uuid4(),
        preferences_json={
            "onboarding_mode": "for_someone_else",
            "preferred_pacing": ["direct", "reflective", "steady"],
            "response_style": ["encouraging"],
            "voice_enabled": True,
        },
        boundaries_json={
            "proactive_check_ins": True,
            "parent_visibility_mode": "full_transcript",
            "alert_threshold": "low",
        },
        routines_json={"daily_cadence": "adaptive"},
    )
    sqlite_session.add(child)
    await sqlite_session.commit()

    context = PortalRequestContext(
        customer_user=customer_user,
        account_id=str(account.id),
        role=HouseholdRole.owner,
        clerk_user_id="user_test",
        clerk_org_id="org_test",
        mfa_verified=True,
        csrf_token="csrf-test",
        container=_FakeContainer(settings),
    )

    app = FastAPI()
    app.include_router(portal.router)
    app.state.container = context.container

    async def _context_override():
        return context

    async def _session_override():
        return sqlite_session

    app.dependency_overrides[require_portal_context] = _context_override
    app.dependency_overrides[get_db_session] = _session_override

    async with httpx.AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        response = await client.get("/app/child")

    assert response.status_code == 200
    assert "Profile Overview" in response.text
    assert "Communication Preferences" in response.text
    assert "Boundaries and Visibility" in response.text
    assert "Routines" in response.text
    assert "Direct, Reflective, Steady" in response.text
    assert "Full transcript" in response.text
    assert "Connected" in response.text
    assert "{&#39;" not in response.text
