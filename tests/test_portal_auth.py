from __future__ import annotations

from app.models.portal import ChildProfile, CustomerUser, Household
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
