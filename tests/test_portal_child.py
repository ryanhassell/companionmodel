from __future__ import annotations

import uuid

import httpx
from fastapi import FastAPI
from httpx import ASGITransport
from sqlalchemy import select

from app.db.session import get_db_session
from app.models.enums import HouseholdRole, SubscriptionStatus
from app.models.persona import Persona
from app.models.portal import Account, ChildProfile, CustomerUser, Household, Subscription
from app.models.user import User
from app.portal.dependencies import PortalRequestContext, require_owner_mfa_context, require_portal_context
from app.routers import portal
from app.services.billing import BillingService


class _FakeClerkAuthService:
    enabled = True


class _FakeContainer:
    def __init__(self, settings) -> None:
        self.settings = settings
        self.clerk_auth_service = _FakeClerkAuthService()
        self.billing_service = BillingService(settings)


class _FakeStripeSubscription:
    retrieved_payload = {"id": "sub_123", "items": {"data": []}}
    modified_calls: list[dict] = []

    @classmethod
    def retrieve(cls, subscription_id):
        return cls.retrieved_payload

    @classmethod
    def modify(cls, subscription_id, **kwargs):
        cls.modified_calls.append({"subscription_id": subscription_id, **kwargs})
        return {"id": subscription_id, "items": {"data": kwargs.get("items", [])}}


class _FakeStripe:
    Subscription = _FakeStripeSubscription


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
    assert "Household Profiles" in response.text
    assert "Add Another Child" in response.text
    assert "Communication Preferences" in response.text
    assert "Boundaries and Visibility" in response.text
    assert "Routines" in response.text
    assert "Choose this child’s Resona" in response.text
    assert "Direct, Reflective, Steady" in response.text
    assert "Full transcript" in response.text
    assert "Connected" in response.text
    assert "{&#39;" not in response.text


async def test_child_profile_page_supports_multiple_children_and_selection(settings, sqlite_session):
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

    first_child = ChildProfile(
        account_id=account.id,
        household_id=household.id,
        first_name="Katie",
        display_name="Katie",
        birth_year=2004,
    )
    second_child = ChildProfile(
        account_id=account.id,
        household_id=household.id,
        first_name="Sophie",
        display_name="Sophie",
        birth_year=2010,
    )
    sqlite_session.add_all([first_child, second_child])
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
        response = await client.get(f"/app/child?child_id={second_child.id}")

    assert response.status_code == 200
    assert "2 profiles" in response.text
    assert "Sophie" in response.text
    assert "Katie" in response.text
    assert f"resona_selected_child={second_child.id}" in response.headers.get("set-cookie", "")


async def test_owner_can_add_additional_child_profile(settings, sqlite_session):
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

    existing_child = ChildProfile(
        account_id=account.id,
        household_id=household.id,
        first_name="Katie",
        display_name="Katie",
        birth_year=2004,
    )
    sqlite_session.add(existing_child)
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
    app.dependency_overrides[require_owner_mfa_context] = _context_override
    app.dependency_overrides[get_db_session] = _session_override

    async with httpx.AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        response = await client.post(
            "/app/child/add",
            data={
                "csrf_token": "csrf-test",
                "next": "/app/child",
                "first_name": "Sophie",
                "display_name": "Sophie",
                "birth_year": "2010",
                "notes": "Loves drawing.",
            },
            follow_redirects=False,
        )

    assert response.status_code == 303
    assert "/app/child?child_id=" in response.headers["location"]

    children = (
        await sqlite_session.execute(
            select(ChildProfile).where(ChildProfile.account_id == account.id).order_by(ChildProfile.created_at)
        )
    ).scalars().all()
    assert len(children) == 2
    assert children[1].display_name == "Sophie"
    assert children[1].birth_year == 2010


async def test_owner_can_update_child_resona_without_affecting_another_child(settings, sqlite_session):
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

    first_companion = User(display_name="Katie", phone_number="+16105550001")
    second_companion = User(display_name="Sophie", phone_number="+16105550002")
    sqlite_session.add_all([first_companion, second_companion])
    await sqlite_session.flush()

    second_persona = Persona(
        key="sophie-custom",
        display_name="Lark",
        account_id=account.id,
        owner_user_id=second_companion.id,
        source_type="portal_custom",
    )
    sqlite_session.add(second_persona)
    await sqlite_session.flush()
    second_companion.preferred_persona_id = second_persona.id

    first_child = ChildProfile(
        account_id=account.id,
        household_id=household.id,
        first_name="Katie",
        display_name="Katie",
        companion_user_id=first_companion.id,
    )
    second_child = ChildProfile(
        account_id=account.id,
        household_id=household.id,
        first_name="Sophie",
        display_name="Sophie",
        companion_user_id=second_companion.id,
    )
    sqlite_session.add_all([first_child, second_child])
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
    app.dependency_overrides[require_owner_mfa_context] = _context_override
    app.dependency_overrides[get_db_session] = _session_override

    async with httpx.AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        response = await client.post(
            f"/app/child/{first_child.id}/resona",
            data={
                "csrf_token": "csrf-test",
                "resona_mode": "preset",
                "resona_preset_key": "juniper",
                "resona_display_name": "Juniper",
                "resona_voice_profile_key": "harbor",
                "resona_vibe": "gentle and steady",
                "resona_support_style": "keep things calm",
                "resona_avoid": "don't rush",
                "resona_anchors": "music, birthdays",
                "resona_proactive_style": "light and familiar",
                "resona_description": "A steady companion for Katie.",
                "resona_style": "Warm and calm.",
                "resona_tone": "Reassuring.",
                "resona_boundaries": "Never push too hard.",
                "resona_topics": "music, birthdays",
                "resona_activities": "check-ins, celebration",
                "resona_speech_style": "simple and kind",
                "resona_disclosure_style": "transparent",
                "resona_texting_length": "short_to_medium",
                "resona_emoji_tendency": "low",
                "resona_parent_notes": "Katie-specific guidance.",
            },
            follow_redirects=False,
        )

    assert response.status_code == 303
    await sqlite_session.refresh(first_companion)
    await sqlite_session.refresh(second_companion)
    await sqlite_session.refresh(first_child)
    await sqlite_session.refresh(second_child)

    first_persona = await sqlite_session.get(Persona, first_companion.preferred_persona_id)
    unchanged_second_persona = await sqlite_session.get(Persona, second_companion.preferred_persona_id)

    assert first_persona is not None
    assert first_persona.display_name == "Juniper"
    assert first_persona.owner_user_id == first_companion.id
    assert first_persona.account_id == account.id
    assert first_persona.source_type == "portal_preset"
    assert first_persona.preset_key == "juniper"
    assert first_child.preferences_json["resona_profile"]["display_name"] == "Juniper"
    assert unchanged_second_persona is not None
    assert unchanged_second_persona.id == second_persona.id
    assert unchanged_second_persona.display_name == "Lark"


async def test_owner_can_update_archive_restore_and_remove_child_profiles(settings, sqlite_session):
    settings.stripe.enabled = True
    settings.stripe.additional_child_price_id = "price_child_addon_123"
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

    primary_child = ChildProfile(
        account_id=account.id,
        household_id=household.id,
        first_name="Katie",
        display_name="Katie",
        birth_year=2004,
    )
    extra_child = ChildProfile(
        account_id=account.id,
        household_id=household.id,
        first_name="Sophie",
        display_name="Sophie",
        birth_year=2010,
    )
    removable_archived = ChildProfile(
        account_id=account.id,
        household_id=household.id,
        first_name="Mia",
        display_name="Mia",
        birth_year=2012,
        is_active=False,
    )
    sqlite_session.add_all([primary_child, extra_child, removable_archived])
    sqlite_session.add(
        Subscription(
            account_id=account.id,
            stripe_subscription_id="sub_123",
            stripe_customer_id="cus_123",
            stripe_price_id="chat_monthly",
            status=SubscriptionStatus.active,
        )
    )
    await sqlite_session.commit()

    _FakeStripeSubscription.retrieved_payload = {
        "id": "sub_123",
        "items": {
            "data": [
                {"id": "si_child_addon", "price": {"id": "price_child_addon_123"}, "quantity": 1},
            ]
        },
    }
    _FakeStripeSubscription.modified_calls = []

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
    context.container.billing_service._stripe = _FakeStripe()

    app = FastAPI()
    app.include_router(portal.router)
    app.state.container = context.container

    async def _context_override():
        return context

    async def _session_override():
        return sqlite_session

    app.dependency_overrides[require_portal_context] = _context_override
    app.dependency_overrides[require_owner_mfa_context] = _context_override
    app.dependency_overrides[get_db_session] = _session_override

    async with httpx.AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        update_response = await client.post(
            f"/app/child/{primary_child.id}/update",
            data={
                "csrf_token": "csrf-test",
                "next": f"/app/child?child_id={primary_child.id}",
                "first_name": "Katie",
                "display_name": "Kate",
                "birth_year": "2005",
                "notes": "Updated notes.",
            },
            follow_redirects=False,
        )
        archive_response = await client.post(
            f"/app/child/{extra_child.id}/archive",
            data={"csrf_token": "csrf-test"},
            follow_redirects=False,
        )
        await sqlite_session.refresh(extra_child)
        assert archive_response.status_code == 303
        assert "archived=1" in archive_response.headers["location"]
        assert extra_child.is_active is False
        _FakeStripeSubscription.retrieved_payload = {"id": "sub_123", "items": {"data": []}}

        restore_response = await client.post(
            f"/app/child/{extra_child.id}/restore",
            data={"csrf_token": "csrf-test"},
            follow_redirects=False,
        )
        await sqlite_session.refresh(extra_child)
        assert restore_response.status_code == 303
        assert "restored=1" in restore_response.headers["location"]
        assert extra_child.is_active is True

        remove_response = await client.post(
            f"/app/child/{removable_archived.id}/remove",
            data={"csrf_token": "csrf-test"},
            follow_redirects=False,
        )

    assert update_response.status_code == 303
    assert "updated=1" in update_response.headers["location"]
    await sqlite_session.refresh(primary_child)
    assert primary_child.display_name == "Kate"
    assert primary_child.birth_year == 2005
    assert primary_child.notes == "Updated notes."

    assert remove_response.status_code == 303
    assert "removed=1" in remove_response.headers["location"]
    assert await sqlite_session.get(ChildProfile, removable_archived.id) is None
    assert len(_FakeStripeSubscription.modified_calls) >= 2
