from __future__ import annotations

import httpx
from fastapi import FastAPI
from httpx import ASGITransport

from app.routers import portal
from app.portal.dependencies import get_optional_portal_context


class _FakeClerkAuthService:
    enabled = True


class _FakeContainer:
    def __init__(self, settings) -> None:
        self.settings = settings
        self.clerk_auth_service = _FakeClerkAuthService()


async def _no_portal_context():
    return None


async def test_login_page_mounts_embedded_clerk_sign_in(settings):
    settings.clerk.enabled = True
    settings.clerk.publishable_key = "pk_test_embedded"
    settings.clerk.frontend_api_url = "https://example.clerk.accounts.dev"

    app = FastAPI()
    app.include_router(portal.router)
    app.state.container = _FakeContainer(settings)
    app.dependency_overrides[get_optional_portal_context] = _no_portal_context

    async with httpx.AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        response = await client.get("/app/login")

    assert response.status_code == 200
    assert 'id="clerk-sign-in-root"' in response.text
    assert 'data-clerk-auth-page="sign-in"' in response.text
    assert 'data-clerk-alternate-url="/app/signup"' in response.text
    assert "/app/session/callback?next=/app/landing" in response.text
    assert "/static/portal-auth.js" in response.text
    assert "@clerk/clerk-js@6.6.0/dist/clerk.browser.js" in response.text


async def test_signup_page_mounts_embedded_clerk_sign_up(settings):
    settings.clerk.enabled = True
    settings.clerk.publishable_key = "pk_test_embedded"
    settings.clerk.frontend_api_url = "https://example.clerk.accounts.dev"

    app = FastAPI()
    app.include_router(portal.router)
    app.state.container = _FakeContainer(settings)
    app.dependency_overrides[get_optional_portal_context] = _no_portal_context

    async with httpx.AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        response = await client.get("/app/signup")

    assert response.status_code == 200
    assert 'id="clerk-sign-up-root"' in response.text
    assert 'data-clerk-auth-page="sign-up"' in response.text
    assert 'data-clerk-alternate-url="/app/login"' in response.text
    assert "/app/session/callback?next=/app/landing" in response.text
    assert "/static/portal-auth.js" in response.text
    assert "@clerk/clerk-js@6.6.0/dist/clerk.browser.js" in response.text


async def test_callback_and_logout_pages_include_session_bridge_routes(settings):
    settings.clerk.enabled = True
    settings.clerk.publishable_key = "pk_test_embedded"
    settings.clerk.frontend_api_url = "https://example.clerk.accounts.dev"

    app = FastAPI()
    app.include_router(portal.router)
    app.state.container = _FakeContainer(settings)
    app.dependency_overrides[get_optional_portal_context] = _no_portal_context

    async with httpx.AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        callback_response = await client.get("/app/session/callback")
        logout_response = await client.get("/app/logout")

    assert callback_response.status_code == 200
    assert "/app/auth/sync" in callback_response.text
    assert logout_response.status_code == 200
    assert "/app/auth/clear" in logout_response.text
    assert "/app/login?signed_out=1" in logout_response.text


async def test_nested_login_and_signup_routes_redirect_to_base(settings):
    settings.clerk.enabled = True
    settings.clerk.publishable_key = "pk_test_embedded"
    settings.clerk.frontend_api_url = "https://example.clerk.accounts.dev"

    app = FastAPI()
    app.include_router(portal.router)
    app.state.container = _FakeContainer(settings)
    app.dependency_overrides[get_optional_portal_context] = _no_portal_context

    async with httpx.AsyncClient(
        transport=ASGITransport(app=app),
        base_url="http://test",
        follow_redirects=False,
    ) as client:
        login_response = await client.get("/app/login/factor-two")
        signup_response = await client.get("/app/signup/continue")

    assert login_response.status_code == 303
    assert login_response.headers["location"] == "/app/login"
    assert signup_response.status_code == 303
    assert signup_response.headers["location"] == "/app/signup"
