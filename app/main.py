from __future__ import annotations

from contextlib import asynccontextmanager
import ipaddress

from fastapi import FastAPI, Request
from fastapi.responses import RedirectResponse
from fastapi.staticfiles import StaticFiles
from uvicorn.middleware.proxy_headers import ProxyHeadersMiddleware

from app.core.logging import configure_logging
from app.core.settings import get_settings
from app.db.session import get_sessionmaker
from app.jobs.scheduler import SchedulerService
from app.routers import admin, api, auth, health, portal, public, webhooks
from app.schemas.site import AdminAccessPolicy
from app.services.container import ServiceContainer

settings = get_settings()
configure_logging(settings)


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
        "default-src 'self'; img-src 'self' data: https:; style-src 'self' 'unsafe-inline'; script-src 'self' 'unsafe-inline'; frame-ancestors 'none'",
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

    public_prefixes = {
        "/app/login",
        "/app/signup",
        "/app/billing/webhook",
        "/app/logout",
    }
    if path == "/app":
        return await call_next(request)
    if any(path == prefix or path.startswith(f"{prefix}/") for prefix in public_prefixes):
        return await call_next(request)

    container: ServiceContainer = request.app.state.container
    if not container.clerk_auth_service.enabled:
        token = request.cookies.get(container.settings.customer_portal.session_cookie_name)
        if not token:
            return RedirectResponse(url="/app/login?reason=auth_required", status_code=303)
        sessionmaker = get_sessionmaker()
        async with sessionmaker() as session:
            authd = await container.customer_auth_service.resolve_portal_session(session, raw_token=token)
            if authd is None:
                response = RedirectResponse(url="/app/login?reason=invalid_session", status_code=303)
                response.delete_cookie(container.settings.customer_portal.session_cookie_name)
                return response
            sub_status = await container.billing_service.account_status(
                session,
                account_id=authd.customer_user.account_id,
            )
            if not container.billing_service.can_access_path(sub_status, path):
                return RedirectResponse(
                    url=f"/app/billing?entitlement=required&status={sub_status.value}",
                    status_code=303,
                )
            await session.commit()
        return await call_next(request)

    token = container.clerk_auth_service.token_from_request(
        request.headers.get("authorization"),
        request.cookies.get(container.settings.clerk.session_cookie_name),
    )
    if not token:
        return RedirectResponse(url="/app/login?reason=auth_required", status_code=303)

    sessionmaker = get_sessionmaker()
    async with sessionmaker() as session:
        try:
            claims = container.clerk_auth_service.verify_token(token)
            tenant = await container.clerk_auth_service.resolve_tenant_context(session, claims)
        except Exception:
            await session.rollback()
            response = RedirectResponse(url="/app/login?reason=invalid_session", status_code=303)
            response.delete_cookie(container.settings.clerk.session_cookie_name)
            return response
        request.state.portal_tenant_context = tenant

        if not tenant.clerk_org_id:
            await session.rollback()
            return RedirectResponse(url="/app/login?reason=no_org", status_code=303)

        sensitive_prefixes = {
            "/app/security",
            "/app/team",
        }
        if (
            tenant.role.value == "owner"
            and settings.clerk.require_owner_mfa
            and any(path.startswith(prefix) for prefix in sensitive_prefixes)
            and not tenant.mfa_verified
        ):
            await session.commit()
            return RedirectResponse(url="/app/login?reason=mfa_required", status_code=303)

        sub_status = await container.billing_service.account_status(
            session,
            account_id=tenant.account.id,
        )
        if not container.billing_service.can_access_path(sub_status, path):
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
