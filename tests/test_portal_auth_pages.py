from __future__ import annotations

import httpx
from fastapi import FastAPI
from httpx import ASGITransport

from app.portal.http import is_portal_interactive_request, portal_json_error_response
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
    assert 'data-clerk-callback-url="/app/session/callback?next=%2Fapp%2Flanding"' in response.text
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
    assert 'data-clerk-callback-url="/app/session/callback?next=%2Fapp%2Flanding"' in response.text
    assert "/static/portal-auth.js" in response.text
    assert "@clerk/clerk-js@6.6.0/dist/clerk.browser.js" in response.text


async def test_auth_pages_preserve_resume_path(settings):
    settings.clerk.enabled = True
    settings.clerk.publishable_key = "pk_test_embedded"
    settings.clerk.frontend_api_url = "https://example.clerk.accounts.dev"

    app = FastAPI()
    app.include_router(portal.router)
    app.state.container = _FakeContainer(settings)
    app.dependency_overrides[get_optional_portal_context] = _no_portal_context

    async with httpx.AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        login_response = await client.get("/app/login", params={"resume": "/app/initialize"})
        signup_response = await client.get("/app/signup", params={"resume": "/app/initialize"})

    assert 'data-clerk-callback-url="/app/session/callback?next=%2Fapp%2Finitialize"' in login_response.text
    assert 'data-clerk-alternate-url="/app/signup?resume=%2Fapp%2Finitialize"' in login_response.text
    assert 'data-clerk-callback-url="/app/session/callback?next=%2Fapp%2Finitialize"' in signup_response.text
    assert 'data-clerk-alternate-url="/app/login?resume=%2Fapp%2Finitialize"' in signup_response.text


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
    assert "/static/portal-callback.js" in callback_response.text
    assert "portal-callback-retry" in callback_response.text
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
        login_response = await client.get("/app/login/factor-two", params={"resume": "/app/initialize"})
        signup_response = await client.get("/app/signup/continue", params={"resume": "/app/initialize"})

    assert login_response.status_code == 303
    assert login_response.headers["location"] == "/app/login?resume=%2Fapp%2Finitialize"
    assert signup_response.status_code == 303
    assert signup_response.headers["location"] == "/app/signup?resume=%2Fapp%2Finitialize"


def test_safe_resume_helpers():
    assert portal._safe_resume_path("/app/initialize") == "/app/initialize"
    assert portal._safe_resume_path("https://example.com") == "/app/landing"
    assert portal._safe_resume_path("//example.com") == "/app/landing"
    assert portal._auth_page_url("/app/login", resume_path="/app/initialize", reason="invalid_session") == "/app/login?resume=%2Fapp%2Finitialize&reason=invalid_session"


def test_portal_interactive_request_and_json_error_response():
    from starlette.requests import Request

    scope = {
        "type": "http",
        "method": "POST",
        "path": "/app/initialize/save",
        "query_string": b"",
        "headers": [
            (b"accept", b"application/json"),
            (b"x-resona-resume-url", b"/app/initialize"),
        ],
        "client": ("127.0.0.1", 1234),
        "scheme": "https",
        "server": ("test", 443),
    }
    starlette_request = Request(scope)
    assert is_portal_interactive_request(starlette_request) is True

    response = portal_json_error_response(
        starlette_request,
        status_code=401,
        code="auth_expired",
        detail="Session expired",
        login_reason="invalid_session",
    )
    body = response.body.decode("utf-8")
    assert '"code":"auth_expired"' in body
    assert '"login_url":"/app/login?reason=invalid_session&resume=%2Fapp%2Finitialize"' in body
