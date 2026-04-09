from __future__ import annotations

import uuid

import httpx
from fastapi import FastAPI
from httpx import ASGITransport

from app.db.session import get_db_session
from app.models.enums import HouseholdRole
from app.models.portal import CustomerUser
from app.portal.dependencies import PortalRequestContext, require_portal_context
from app.routers import portal


class _FakeClerkAuthService:
    enabled = True

    def __init__(self, *, should_verify: bool = True) -> None:
        self.should_verify = should_verify
        self.calls: list[tuple[str, str]] = []

    async def verify_current_password(self, *, clerk_user_id: str, password: str) -> bool:
        self.calls.append((clerk_user_id, password))
        return self.should_verify


class _FakeContainer:
    def __init__(self, settings, clerk_auth_service) -> None:
        self.settings = settings
        self.clerk_auth_service = clerk_auth_service


def _build_context(settings, clerk_auth_service: _FakeClerkAuthService) -> PortalRequestContext:
    account_id = uuid.uuid4()
    customer_user = CustomerUser(
        id=uuid.uuid4(),
        account_id=account_id,
        email="parent@example.com",
        password_hash="clerk:test",
        display_name="Ryan",
    )
    container = _FakeContainer(settings, clerk_auth_service)
    return PortalRequestContext(
        customer_user=customer_user,
        account_id=str(account_id),
        role=HouseholdRole.owner,
        clerk_user_id="user_123",
        clerk_org_id="org_123",
        mfa_verified=True,
        csrf_token="csrf-test",
        container=container,
    )


async def test_security_page_requires_password_before_showing_clerk_controls(settings, sqlite_session):
    settings.clerk.enabled = True
    settings.clerk.publishable_key = "pk_test_embedded"
    settings.clerk.frontend_api_url = "https://example.clerk.accounts.dev"

    clerk_auth_service = _FakeClerkAuthService()
    context = _build_context(settings, clerk_auth_service)

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
        response = await client.get("/app/security")

    assert response.status_code == 200
    assert "Enter your current password" in response.text
    assert 'action="/app/security/confirm"' in response.text
    assert "id=\"clerk-user-profile-root\"" not in response.text


async def test_security_confirm_sets_unlock_cookie(settings, sqlite_session):
    settings.clerk.enabled = True
    settings.clerk.publishable_key = "pk_test_embedded"
    settings.clerk.frontend_api_url = "https://example.clerk.accounts.dev"
    settings.clerk.secret_key = "sk_test_security"

    clerk_auth_service = _FakeClerkAuthService(should_verify=True)
    context = _build_context(settings, clerk_auth_service)

    app = FastAPI()
    app.include_router(portal.router)
    app.state.container = context.container

    async def _context_override():
        return context

    async def _session_override():
        return sqlite_session

    app.dependency_overrides[require_portal_context] = _context_override
    app.dependency_overrides[get_db_session] = _session_override

    async with httpx.AsyncClient(
        transport=ASGITransport(app=app),
        base_url="https://test",
        follow_redirects=False,
    ) as client:
        response = await client.post(
            "/app/security/confirm",
            data={"csrf_token": "csrf-test", "password": "correct horse"},
        )

    assert response.status_code == 303
    assert response.headers["location"] == "/app/security?confirmed=1"
    assert portal._SECURITY_CONFIRM_COOKIE in response.headers.get("set-cookie", "")
    assert clerk_auth_service.calls == [("user_123", "correct horse")]


async def test_security_confirm_rejects_wrong_password(settings, sqlite_session):
    settings.clerk.enabled = True
    settings.clerk.publishable_key = "pk_test_embedded"
    settings.clerk.frontend_api_url = "https://example.clerk.accounts.dev"
    settings.clerk.secret_key = "sk_test_security"

    clerk_auth_service = _FakeClerkAuthService(should_verify=False)
    context = _build_context(settings, clerk_auth_service)

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
        response = await client.post(
            "/app/security/confirm",
            data={"csrf_token": "csrf-test", "password": "wrong password"},
        )

    assert response.status_code == 400
    assert "unlock account security" in response.text
    assert "current Clerk password" in response.text
    assert "id=\"clerk-user-profile-root\"" not in response.text


async def test_security_page_mounts_clerk_user_profile_after_confirmation(settings, sqlite_session):
    settings.clerk.enabled = True
    settings.clerk.publishable_key = "pk_test_embedded"
    settings.clerk.frontend_api_url = "https://example.clerk.accounts.dev"

    clerk_auth_service = _FakeClerkAuthService()
    context = _build_context(settings, clerk_auth_service)

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
        client.cookies.set(portal._SECURITY_CONFIRM_COOKIE, portal._create_security_confirm_token(context), path="/app")
        response = await client.get("/app/security")

    assert response.status_code == 200
    assert "id=\"clerk-user-profile-root\"" in response.text
    assert "/static/portal-security.js" in response.text
    assert "Manage password, MFA, and sessions" not in response.text
    assert "Unlocked" not in response.text


async def test_invalid_security_cookie_falls_back_to_locked_page(settings, sqlite_session):
    settings.clerk.enabled = True
    settings.clerk.publishable_key = "pk_test_embedded"
    settings.clerk.frontend_api_url = "https://example.clerk.accounts.dev"

    clerk_auth_service = _FakeClerkAuthService()
    context = _build_context(settings, clerk_auth_service)

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
        client.cookies.set(portal._SECURITY_CONFIRM_COOKIE, "bad-token", path="/app")
        response = await client.get("/app/security")

    assert response.status_code == 200
    assert "Enter your current password" in response.text
    assert "id=\"clerk-user-profile-root\"" not in response.text


async def test_security_page_allows_non_owner_for_personal_account_security(settings, sqlite_session):
    settings.clerk.enabled = True
    settings.clerk.publishable_key = "pk_test_embedded"
    settings.clerk.frontend_api_url = "https://example.clerk.accounts.dev"

    clerk_auth_service = _FakeClerkAuthService()
    context = _build_context(settings, clerk_auth_service)
    context.role = HouseholdRole.guardian

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
        response = await client.get("/app/security")

    assert response.status_code == 200
    assert "Enter your current password" in response.text
    assert 'action="/app/security/confirm"' in response.text


async def test_security_page_still_allows_password_gate_without_mfa_verified(settings, sqlite_session):
    settings.clerk.enabled = True
    settings.clerk.publishable_key = "pk_test_embedded"
    settings.clerk.frontend_api_url = "https://example.clerk.accounts.dev"
    settings.clerk.require_owner_mfa = True

    clerk_auth_service = _FakeClerkAuthService()
    context = _build_context(settings, clerk_auth_service)
    context.mfa_verified = False

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
        response = await client.get("/app/security")

    assert response.status_code == 200
    assert "Enter your current password" in response.text
    assert "id=\"clerk-user-profile-root\"" not in response.text
