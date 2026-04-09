from __future__ import annotations

import ipaddress
from contextlib import asynccontextmanager
from urllib.parse import urlparse

from fastapi import FastAPI, Request
from fastapi.responses import RedirectResponse
from fastapi.staticfiles import StaticFiles
from uvicorn.middleware.proxy_headers import ProxyHeadersMiddleware

from app.core.logging import configure_logging
from app.core.settings import get_settings
from app.db.session import get_sessionmaker
from app.portal.dependencies import resolve_clerk_portal_session
from app.jobs.scheduler import SchedulerService
from app.portal.http import is_portal_interactive_request, portal_json_error_response
from app.routers import admin, api, auth, health, portal, public, webhooks
from app.schemas.site import AdminAccessPolicy
from app.services.container import ServiceContainer

settings = get_settings()
configure_logging(settings)

_csp_script_sources = {"'self'", "'unsafe-inline'", "https://cdn.jsdelivr.net", "https://esm.sh"}
_csp_connect_sources = {"'self'", "https://clerk-telemetry.com"}
_csp_frame_sources = {"'self'"}
_clerk_security_sources = {
    "https://challenges.cloudflare.com",
    "https://*.hcaptcha.com",
    "https://hcaptcha.com",
    "https://www.recaptcha.net",
    "https://www.google.com",
    "https://www.gstatic.com",
}
_csp_script_sources.update(_clerk_security_sources)
_csp_connect_sources.update(_clerk_security_sources)
_csp_frame_sources.update(_clerk_security_sources)

if settings.clerk.frontend_api_url:
    parsed_frontend_api = urlparse(settings.clerk.frontend_api_url)
    clerk_origin = f"{parsed_frontend_api.scheme}://{parsed_frontend_api.netloc}"
    _csp_script_sources.add(clerk_origin)
    _csp_connect_sources.add(clerk_origin)
    _csp_frame_sources.add(clerk_origin)

if settings.clerk.issuer:
    parsed_issuer = urlparse(settings.clerk.issuer)
    if parsed_issuer.scheme and parsed_issuer.netloc:
        issuer_origin = f"{parsed_issuer.scheme}://{parsed_issuer.netloc}"
        _csp_script_sources.add(issuer_origin)
        _csp_connect_sources.add(issuer_origin)
        _csp_frame_sources.add(issuer_origin)

_content_security_policy = (
    "default-src 'self'; "
    "img-src 'self' data: https:; "
    "style-src 'self' 'unsafe-inline'; "
    f"script-src {' '.join(sorted(_csp_script_sources))}; "
    "font-src 'self' data: https:; "
    f"connect-src {' '.join(sorted(_csp_connect_sources))}; "
    f"frame-src {' '.join(sorted(_csp_frame_sources))}; "
    "worker-src 'self' blob:; "
    "frame-ancestors 'none'"
)


@asynccontextmanager
async def lifespan(application: FastAPI):
    settings.media_root_path.mkdir(parents=True, exist_ok=True)
    settings.log_path.parent.mkdir(parents=True, exist_ok=True)
    container = ServiceContainer.build(settings)
    await container.rate_limiter_service.initialize()
    scheduler_service = SchedulerService(settings, container)
    container.scheduler_service = scheduler_service
    application.state.container = container
    application.state.scheduler_service = scheduler_service
    scheduler_service.start()
    try:
        yield
    finally:
        scheduler_service.shutdown()
        await container.aclose()


app = FastAPI(title=settings.app.name, lifespan=lifespan)
if settings.app.trust_proxy_headers:
    app.add_middleware(ProxyHeadersMiddleware, trusted_hosts="*")

app.mount("/static", StaticFiles(directory="app/static"), name="static")


def _extract_request_ip(request: Request) -> str | None:
    forwarded = request.headers.get("x-forwarded-for")
    if forwarded:
        return forwarded.split(",")[0].strip()
    if request.client:
        return request.client.host
    return None


def _is_allowed_admin_request(request: Request) -> bool:
    policy = AdminAccessPolicy(
        internal_only=settings.admin.internal_only,
        allowlist_cidrs=settings.admin.allowlist_cidrs,
        trusted_header_name=settings.admin.trusted_header_name,
    )
    header_name = policy.trusted_header_name.lower()
    if settings.admin.trusted_header_value:
        if request.headers.get(header_name) == settings.admin.trusted_header_value:
            return True
    ip_text = _extract_request_ip(request)
    if not ip_text:
        return False
    try:
        request_ip = ipaddress.ip_address(ip_text)
    except ValueError:
        return False
    for cidr in policy.allowlist_cidrs:
        try:
            if request_ip in ipaddress.ip_network(cidr, strict=False):
                return True
        except ValueError:
            continue
    return False


@app.middleware("http")
async def security_headers_middleware(request: Request, call_next):
    response = await call_next(request)
    response.headers.setdefault("X-Content-Type-Options", "nosniff")
    response.headers.setdefault("X-Frame-Options", "DENY")
    response.headers.setdefault("Referrer-Policy", "strict-origin-when-cross-origin")
    response.headers.setdefault(
        "Content-Security-Policy",
        _content_security_policy,
    )
    return response


@app.middleware("http")
async def admin_internal_only_middleware(request: Request, call_next):
    if not settings.admin.internal_only:
        return await call_next(request)
    path = request.url.path
    protected_prefixes = ("/admin", "/login", "/bootstrap")
    protected_exact = {"/logout"}
    if not (path.startswith(protected_prefixes) or path in protected_exact):
        return await call_next(request)
    if not _is_allowed_admin_request(request):
        return RedirectResponse(url="/", status_code=303)
    return await call_next(request)


@app.middleware("http")
async def portal_entitlement_middleware(request: Request, call_next):
    if not settings.customer_portal.enabled:
        return await call_next(request)
    path = request.url.path
    if not path.startswith("/app"):
        return await call_next(request)
    wants_json = is_portal_interactive_request(request)

    public_prefixes = {
        "/app/login",
        "/app/signup",
        "/app/session/callback",
        "/app/auth/sync",
        "/app/auth/clear",
        "/app/billing/webhook",
        "/app/logout",
    }
    initialization_prefixes = {
        "/app/initialize",
        "/app/initialize/save",
        "/app/initialize/billing/checkout",
        "/app/initialize/return",
    }
    if path == "/app":
        return await call_next(request)
    if any(path == prefix or path.startswith(f"{prefix}/") for prefix in public_prefixes):
        return await call_next(request)

    container: ServiceContainer = request.app.state.container
    if not container.clerk_auth_service.enabled:
        token = request.cookies.get(container.settings.customer_portal.session_cookie_name)
        if not token:
            if wants_json:
                return portal_json_error_response(
                    request,
                    status_code=401,
                    code="auth_expired",
                    detail="A portal session is required to continue.",
                    login_reason="auth_required",
                )
            return RedirectResponse(url="/app/login?reason=auth_required", status_code=303)
        sessionmaker = get_sessionmaker()
        async with sessionmaker() as session:
            authd = await container.customer_auth_service.resolve_portal_session(session, raw_token=token)
            if authd is None:
                if wants_json:
                    response = portal_json_error_response(
                        request,
                        status_code=401,
                        code="auth_expired",
                        detail="Your portal session expired.",
                        login_reason="invalid_session",
                    )
                    response.delete_cookie(container.settings.customer_portal.session_cookie_name)
                    return response
                response = RedirectResponse(url="/app/login?reason=invalid_session", status_code=303)
                response.delete_cookie(container.settings.customer_portal.session_cookie_name)
                return response
            init_result = await container.portal_initialization_service.load_context(
                session,
                customer_user=authd.customer_user,
            )
            is_initialization_path = any(
                path == prefix or path.startswith(f"{prefix}/") for prefix in initialization_prefixes
            )
            if container.portal_initialization_service.requires_initialization(init_result.context) and not is_initialization_path:
                await session.commit()
                return RedirectResponse(url="/app/initialize", status_code=303)
            if is_initialization_path:
                await session.commit()
                return await call_next(request)
            sub_status = await container.billing_service.account_status(
                session,
                account_id=authd.customer_user.account_id,
            )
            if not container.billing_service.can_access_path(sub_status, path):
                if wants_json:
                    return portal_json_error_response(
                        request,
                        status_code=403,
                        code="entitlement_required",
                        detail="Billing access is required before this action can continue.",
                        login_reason="auth_required",
                    )
                return RedirectResponse(
                    url=f"/app/billing?entitlement=required&status={sub_status.value}",
                    status_code=303,
                )
            await session.commit()
        return await call_next(request)

    sessionmaker = get_sessionmaker()
    async with sessionmaker() as session:
        context, _ = await resolve_clerk_portal_session(request, session, container)
        if context is None:
            await session.rollback()
            if wants_json:
                response = portal_json_error_response(
                    request,
                    status_code=401,
                    code="auth_expired",
                    detail="Your secure session expired.",
                    login_reason="invalid_session",
                )
                response.delete_cookie(container.settings.clerk.session_cookie_name)
                response.delete_cookie(container.settings.clerk.backend_session_cookie_name)
                response.delete_cookie(container.settings.customer_portal.session_cookie_name)
                return response
            response = RedirectResponse(url="/app/login?reason=invalid_session", status_code=303)
            response.delete_cookie(container.settings.clerk.session_cookie_name)
            response.delete_cookie(container.settings.clerk.backend_session_cookie_name)
            response.delete_cookie(container.settings.customer_portal.session_cookie_name)
            return response
        tenant = request.state.portal_tenant_context

        if not tenant.clerk_org_id:
            await session.rollback()
            if wants_json:
                return portal_json_error_response(
                    request,
                    status_code=403,
                    code="no_org",
                    detail="Select or create an organization to continue.",
                    login_reason="no_org",
                )
            return RedirectResponse(url="/app/login?reason=no_org", status_code=303)

        init_result = await container.portal_initialization_service.load_context(
            session,
            customer_user=tenant.customer_user,
        )
        is_initialization_path = any(
            path == prefix or path.startswith(f"{prefix}/") for prefix in initialization_prefixes
        )
        if container.portal_initialization_service.requires_initialization(init_result.context) and not is_initialization_path:
            await session.commit()
            return RedirectResponse(url="/app/initialize", status_code=303)
        if is_initialization_path:
            await session.commit()
            return await call_next(request)

        sensitive_prefixes = {
            "/app/team",
        }
        if (
            tenant.role.value == "owner"
            and settings.clerk.require_owner_mfa
            and any(path.startswith(prefix) for prefix in sensitive_prefixes)
            and not tenant.mfa_verified
        ):
            await session.commit()
            if wants_json:
                return portal_json_error_response(
                    request,
                    status_code=403,
                    code="mfa_required",
                    detail="Multi-factor authentication is required for this action.",
                    login_reason="mfa_required",
                )
            return RedirectResponse(url="/app/login?reason=mfa_required", status_code=303)

        sub_status = await container.billing_service.account_status(
            session,
            account_id=tenant.account.id,
        )
        if not container.billing_service.can_access_path(sub_status, path):
            if wants_json:
                return portal_json_error_response(
                    request,
                    status_code=403,
                    code="entitlement_required",
                    detail="Billing access is required before this action can continue.",
                    login_reason="auth_required",
                )
            return RedirectResponse(
                url=f"/app/billing?entitlement=required&status={sub_status.value}",
                status_code=303,
            )
        await session.commit()

    return await call_next(request)


app.include_router(auth.router)
app.include_router(public.router)
app.include_router(health.router)
app.include_router(webhooks.router)
app.include_router(api.router)
app.include_router(admin.router)
app.include_router(portal.router)
