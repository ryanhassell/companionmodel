from __future__ import annotations

import hmac
from typing import Any
from urllib.parse import urlencode

from fastapi import APIRouter, Depends, HTTPException, Request, status
from fastapi.responses import JSONResponse, RedirectResponse
from itsdangerous import BadSignature, URLSafeTimedSerializer
from sqlalchemy import desc, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.logging import get_logger
from app.core.settings import RuntimeSettings
from app.core.templating import templates
from app.db.session import get_db_session
from app.models.enums import MemoryType
from app.models.communication import Message, SafetyEvent
from app.models.memory import MemoryItem
from app.models.portal import Account, ChildProfile, CustomerUser, Household
from app.portal.dependencies import (
    PortalRequestContext,
    get_optional_portal_context,
    require_owner_mfa_context,
    require_portal_context,
)
from app.schemas.site import ParentDashboardContext, PortalNavItem, PortalNavSection
from app.services.portal_initialization import InitializationValidationError
from app.utils.time import utc_now

router = APIRouter(prefix="/app", tags=["portal"])
logger = get_logger(__name__)
_SECURITY_CONFIRM_COOKIE = "resona_security_confirmed"
_SECURITY_CONFIRM_SALT = "portal-security-confirmed"
_SECURITY_CONFIRM_MAX_AGE_SECONDS = 600


def _security_confirm_serializer(settings: RuntimeSettings) -> URLSafeTimedSerializer:
    return URLSafeTimedSerializer(settings.app.secret_key, salt=_SECURITY_CONFIRM_SALT)


def _create_security_confirm_token(context: PortalRequestContext) -> str:
    payload = {
        "clerk_user_id": context.clerk_user_id,
        "clerk_org_id": context.clerk_org_id,
        "account_id": context.account_id,
    }
    return _security_confirm_serializer(context.container.settings).dumps(payload)


def _security_gate_unlocked(request: Request, context: PortalRequestContext) -> bool:
    token = request.cookies.get(_SECURITY_CONFIRM_COOKIE)
    if not token:
        return False
    try:
        payload = _security_confirm_serializer(context.container.settings).loads(
            token,
            max_age=_SECURITY_CONFIRM_MAX_AGE_SECONDS,
        )
    except BadSignature:
        return False
    expected = {
        "clerk_user_id": context.clerk_user_id,
        "clerk_org_id": context.clerk_org_id,
        "account_id": context.account_id,
    }
    for key, value in expected.items():
        if not hmac.compare_digest(str(payload.get(key, "")), str(value)):
            return False
    return True


def _clear_security_confirm_cookie(response: RedirectResponse | JSONResponse | object) -> None:
    if hasattr(response, "delete_cookie"):
        response.delete_cookie(_SECURITY_CONFIRM_COOKIE, path="/app")


def _customer_display_name(customer_user: CustomerUser | None) -> str:
    if customer_user is None:
        return "My account"
    display_name = (customer_user.display_name or "").strip()
    if display_name and "@clerk.local" not in display_name.lower():
        return display_name
    email = (customer_user.email or "").strip()
    if email and "@clerk.local" not in email.lower():
        return email
    return "My account"


def _client_ip(request: Request) -> str | None:
    forwarded = request.headers.get("x-forwarded-for")
    if forwarded:
        return forwarded.split(",")[0].strip()
    if request.client:
        return request.client.host
    return None


def _portal_response(request: Request, template: str, **context):
    status_code = int(context.pop("status_code", 200))
    settings = request.app.state.container.settings
    nav_sections = [
        PortalNavSection(
            key="overview",
            label="Overview",
            items=[
                PortalNavItem(href="/app/landing", label="Portal Home", key="landing"),
                PortalNavItem(href="/app/dashboard", label="Dashboard", key="dashboard"),
            ],
        ),
        PortalNavSection(
            key="conversations",
            label="Conversations",
            items=[
                PortalNavItem(href="/app/timeline", label="Timeline", key="timeline"),
            ],
        ),
        PortalNavSection(
            key="memories",
            label="Memories",
            items=[
                PortalNavItem(href="/app/memories/map", label="Memory Map", key="memories-map"),
                PortalNavItem(href="/app/memories/library", label="Memory Library", key="memories-library"),
            ],
        ),
        PortalNavSection(
            key="safety",
            label="Safety",
            items=[
                PortalNavItem(href="/app/safety", label="Safety Feed", key="safety"),
            ],
        ),
        PortalNavSection(
            key="household",
            label="Household",
            items=[
                PortalNavItem(href="/app/child", label="Child Profile", key="child"),
                PortalNavItem(href="/app/team", label="Team", key="team"),
                PortalNavItem(href="/app/initialize", label="Setup", key="initialize"),
                PortalNavItem(href="/app/security", label="Security", key="security"),
            ],
        ),
        PortalNavSection(
            key="billing",
            label="Billing",
            items=[
                PortalNavItem(href="/app/billing", label="Billing", key="billing"),
            ],
        ),
    ]
    payload = {
        "request": request,
        "brand_name": settings.web.brand_name,
        "support_email": settings.web.support_email,
        "privacy_url": settings.web.privacy_url,
        "terms_url": settings.web.terms_url,
        "safety_policy_url": settings.web.safety_policy_url,
        "clerk_enabled": request.app.state.container.clerk_auth_service.enabled,
        "clerk_publishable_key": settings.clerk.publishable_key,
        "clerk_frontend_api_url": settings.clerk.frontend_api_url,
        "portal_nav_sections": nav_sections,
        **context,
    }
    payload["customer_display_name"] = _customer_display_name(payload.get("customer_user"))
    return templates.TemplateResponse(template, payload, status_code=status_code)


def _safe_resume_path(path: str | None, *, default: str = "/app/landing") -> str:
    candidate = str(path or "").strip()
    if not candidate:
        return default
    if not candidate.startswith("/"):
        return default
    if candidate.startswith("//"):
        return default
    return candidate


def _clerk_callback_url(next_path: str = "/app/landing") -> str:
    return f"/app/session/callback?{urlencode({'next': _safe_resume_path(next_path)})}"


def _auth_page_url(base_path: str, *, resume_path: str | None = None, **params: str | None) -> str:
    query: dict[str, str] = {}
    safe_resume = _safe_resume_path(resume_path, default="/app/landing")
    if safe_resume != "/app/landing":
        query["resume"] = safe_resume
    for key, value in params.items():
        normalized = str(value or "").strip()
        if normalized:
            query[key] = normalized
    if not query:
        return base_path
    return f"{base_path}?{urlencode(query)}"


def _portal_auth_page_response(request: Request, template: str, **context):
    container = request.app.state.container
    resume_path = _safe_resume_path(request.query_params.get("resume"))
    is_login = template.endswith("login.html")
    alternate_path = "/app/signup" if is_login else "/app/login"
    return _portal_response(
        request,
        template,
        legacy_auth_enabled=not container.clerk_auth_service.enabled,
        clerk_enabled=container.clerk_auth_service.enabled,
        clerk_publishable_key=container.settings.clerk.publishable_key,
        clerk_frontend_api_url=container.settings.clerk.frontend_api_url,
        clerk_callback_url=_clerk_callback_url(resume_path),
        auth_resume_path=resume_path,
        auth_alternate_url=_auth_page_url(alternate_path, resume_path=resume_path),
        **context,
    )


def _initialization_next_step(current_step: str, completed_steps: list[str], step_order: list[str]) -> str:
    completed = set(completed_steps)
    if current_step == "complete":
        return "complete"
    try:
        start_idx = step_order.index(current_step)
    except ValueError:
        start_idx = 0
    for step in step_order[start_idx + 1 :]:
        if step not in completed or step == "complete":
            return step
    return step_order[-1]


def _initialization_previous_step(current_step: str, step_order: list[str]) -> str:
    try:
        idx = step_order.index(current_step)
    except ValueError:
        return step_order[0]
    return step_order[max(idx - 1, 0)]


def _humanize_choice(value: str | None) -> str:
    cleaned = str(value or "").strip()
    if not cleaned:
        return "Not set"
    custom = {
        "for_myself": "For myself",
        "for_someone_else": "For someone else",
        "full_transcript": "Full transcript",
        "summary_only": "Summary only",
        "event_only": "Safety events only",
        "low": "Low",
        "medium": "Medium",
        "high": "High",
        "adaptive": "Adaptive",
        "voice_enabled": "Voice enabled",
    }
    if cleaned in custom:
        return custom[cleaned]
    return cleaned.replace("_", " ").strip().title()


def _humanize_profile_value(value: Any) -> str:
    if value is None:
        return "Not set"
    if isinstance(value, bool):
        return "Yes" if value else "No"
    if isinstance(value, list):
        cleaned = [_humanize_choice(str(item)) for item in value if str(item).strip()]
        return ", ".join(cleaned) if cleaned else "Not set"
    if isinstance(value, str):
        stripped = value.strip()
        if not stripped:
            return "Not set"
        return _humanize_choice(stripped)
    return str(value)


def _child_profile_sections(child: ChildProfile) -> list[dict[str, Any]]:
    preferences = child.preferences_json or {}
    boundaries = child.boundaries_json or {}
    routines = child.routines_json or {}

    sections: list[dict[str, Any]] = [
        {
            "title": "Communication Preferences",
            "items": [
                {"label": "Setup mode", "value": _humanize_profile_value(preferences.get("onboarding_mode"))},
                {"label": "Preferred pacing", "value": _humanize_profile_value(preferences.get("preferred_pacing"))},
                {"label": "Custom pacing guidance", "value": _humanize_profile_value(preferences.get("preferred_pacing_custom"))},
                {"label": "Response style", "value": _humanize_profile_value(preferences.get("response_style"))},
                {"label": "Custom tone guidance", "value": _humanize_profile_value(preferences.get("response_style_custom"))},
                {"label": "Voice enabled", "value": _humanize_profile_value(preferences.get("voice_enabled"))},
                {"label": "Additional communication notes", "value": _humanize_profile_value(preferences.get("communication_notes"))},
            ],
        },
        {
            "title": "Boundaries and Visibility",
            "items": [
                {"label": "Proactive check-ins", "value": _humanize_profile_value(boundaries.get("proactive_check_ins"))},
                {"label": "Parent visibility", "value": _humanize_profile_value(boundaries.get("parent_visibility_mode"))},
                {"label": "Alert threshold", "value": _humanize_profile_value(boundaries.get("alert_threshold"))},
            ],
        },
        {
            "title": "Routines",
            "items": [
                {"label": "Daily cadence", "value": _humanize_profile_value(routines.get("daily_cadence"))},
                {"label": "Quiet hours", "value": _humanize_profile_value(routines.get("quiet_hours"))},
            ],
        },
    ]

    for section in sections:
        section["items"] = [
            item
            for item in section["items"]
            if item["value"] != "Not set" or item["label"] in {"Voice enabled", "Proactive check-ins"}
        ]
    return [section for section in sections if section["items"]]


async def _portal_child_profile(session: AsyncSession, *, account_id) -> ChildProfile | None:
    return await session.scalar(select(ChildProfile).where(ChildProfile.account_id == account_id))


def _memory_row_payload(item: MemoryItem) -> dict[str, Any]:
    title = (item.title or "").strip() or (item.summary or "").strip() or (item.content or "").strip()[:72] or "Memory"
    summary = (item.summary or "").strip() or (item.content or "").strip()
    if len(summary) > 180:
        summary = f"{summary[:177].rstrip()}..."
    updated_label = item.updated_at.strftime("%b %d, %Y at %I:%M %p") if item.updated_at else "Recently updated"
    return {
        "id": str(item.id),
        "title": title,
        "summary": summary,
        "memory_type": item.memory_type.value,
        "memory_type_label": item.memory_type.value.replace("_", " ").title(),
        "updated_at": item.updated_at.isoformat() if item.updated_at else None,
        "updated_label": updated_label,
        "pinned": bool(item.pinned),
        "archived": bool(item.disabled),
        "tags": list(item.tags or []),
    }


def _memory_filter_options() -> list[dict[str, str]]:
    return [
        {"value": "", "label": "All memory types"},
        *[
            {"value": memory_type.value, "label": memory_type.value.replace("_", " ").title()}
            for memory_type in MemoryType
        ],
    ]


@router.get("")
async def portal_root(
    request: Request,
    session: AsyncSession = Depends(get_db_session),
    context: PortalRequestContext | None = Depends(get_optional_portal_context),
):
    if context is None:
        return RedirectResponse(
            url=_auth_page_url("/app/login", resume_path="/app/initialize"),
            status_code=303,
        )
    init_result = await context.container.portal_initialization_service.load_context(
        session,
        customer_user=context.customer_user,
    )
    await session.commit()
    if context.container.portal_initialization_service.requires_initialization(init_result.context):
        return RedirectResponse(url="/app/initialize", status_code=303)
    return RedirectResponse(url="/app/landing", status_code=303)


@router.get("/landing")
async def portal_landing(
    request: Request,
    session: AsyncSession = Depends(get_db_session),
    context: PortalRequestContext = Depends(require_portal_context),
):
    household = await session.scalar(select(Household).where(Household.account_id == context.customer_user.account_id))
    return _portal_response(
        request,
        "portal/landing.html",
        active_nav="landing",
        customer_user=context.customer_user,
        mfa_verified=context.mfa_verified,
        household=household,
    )


@router.get("/signup")
async def portal_signup_page(
    request: Request,
    context: PortalRequestContext | None = Depends(get_optional_portal_context),
):
    if context is not None:
        destination = _safe_resume_path(request.query_params.get("resume"))
        return RedirectResponse(url=destination, status_code=303)
    return _portal_auth_page_response(
        request,
        "portal/signup.html",
    )


@router.get("/signup/{_clerk_path:path}")
async def portal_signup_page_catchall(
    request: Request,
    _clerk_path: str,
    context: PortalRequestContext | None = Depends(get_optional_portal_context),
):
    return RedirectResponse(url=_auth_page_url("/app/signup", resume_path=request.query_params.get("resume")), status_code=303)


@router.post("/signup")
async def portal_signup_submit(
    request: Request,
    session: AsyncSession = Depends(get_db_session),
):
    container = request.app.state.container
    ip = _client_ip(request) or "unknown"
    signup_limit = container.settings.rate_limit
    await _enforce_rate_limit(
        request,
        key=f"signup:preauth:{ip}",
        limit=signup_limit.signup_limit,
        window_seconds=signup_limit.signup_window_seconds,
    )
    if not container.clerk_auth_service.enabled:
        form = await request.form()
        try:
            customer_user, email_token, otp_code = await container.customer_auth_service.register_user(
                session,
                email=str(form.get("email", "")),
                password=str(form.get("password", "")),
                display_name=str(form.get("display_name", "")),
                phone_number=str(form.get("phone_number", "")).strip() or None,
                accepted_terms=bool(form.get("accepted_terms")),
                accepted_privacy=bool(form.get("accepted_privacy")),
                ip_address=ip,
                user_agent=request.headers.get("user-agent"),
            )
            token, _ = await container.customer_auth_service.create_portal_session(
                session,
                customer_user=customer_user,
                user_agent=request.headers.get("user-agent"),
                ip_address=ip,
                trusted_device=bool(form.get("trusted_device")),
            )
        except ValueError as exc:
            return _portal_response(
                request,
                "portal/signup.html",
                legacy_auth_enabled=True,
                error=str(exc),
                status_code=400,
            )
        await container.notification_service.send_verification_email(
            to_email=customer_user.email,
            display_name=customer_user.display_name,
            verify_token=email_token,
        )
        if otp_code and customer_user.phone_number:
            await container.notification_service.send_verification_sms(
                to_number=customer_user.phone_number,
                otp_code=otp_code,
            )
        await session.commit()
        response = RedirectResponse(url="/app/verify", status_code=303)
        response.set_cookie(
            container.settings.customer_portal.session_cookie_name,
            token,
            httponly=True,
            secure=container.settings.customer_portal.secure_cookies,
            samesite="lax",
            max_age=container.settings.customer_portal.session_max_age_seconds,
        )
        return response
    return RedirectResponse(
        url=_auth_page_url("/app/signup", resume_path=request.query_params.get("resume")),
        status_code=303,
    )


@router.get("/login")
async def portal_login_page(
    request: Request,
    context: PortalRequestContext | None = Depends(get_optional_portal_context),
):
    if context is not None:
        destination = _safe_resume_path(request.query_params.get("resume"))
        return RedirectResponse(url=destination, status_code=303)
    return _portal_auth_page_response(
        request,
        "portal/login.html",
        reason=request.query_params.get("reason"),
    )


@router.get("/login/{_clerk_path:path}")
async def portal_login_page_catchall(
    request: Request,
    _clerk_path: str,
    context: PortalRequestContext | None = Depends(get_optional_portal_context),
):
    return RedirectResponse(url=_auth_page_url("/app/login", resume_path=request.query_params.get("resume")), status_code=303)


@router.post("/login")
async def portal_login_submit(
    request: Request,
    session: AsyncSession = Depends(get_db_session),
):
    container = request.app.state.container
    ip = _client_ip(request) or "unknown"
    login_limit = container.settings.rate_limit
    await _enforce_rate_limit(
        request,
        key=f"login:preauth:{ip}",
        limit=login_limit.login_limit,
        window_seconds=login_limit.login_window_seconds,
    )
    if not container.clerk_auth_service.enabled:
        form = await request.form()
        customer_user = await container.customer_auth_service.authenticate(
            session,
            email=str(form.get("email", "")),
            password=str(form.get("password", "")),
        )
        if customer_user is None:
            await session.commit()
            return _portal_response(
                request,
                "portal/login.html",
                legacy_auth_enabled=True,
                error="Invalid credentials or temporary lockout in effect.",
                status_code=400,
            )
        token, _ = await container.customer_auth_service.create_portal_session(
            session,
            customer_user=customer_user,
            user_agent=request.headers.get("user-agent"),
            ip_address=ip,
            trusted_device=bool(form.get("trusted_device")),
        )
        await session.commit()
        response = RedirectResponse(url="/app/landing", status_code=303)
        response.set_cookie(
            container.settings.customer_portal.session_cookie_name,
            token,
            httponly=True,
            secure=container.settings.customer_portal.secure_cookies,
            samesite="lax",
            max_age=container.settings.customer_portal.session_max_age_seconds,
        )
        return response
    return RedirectResponse(url=container.settings.clerk.sign_in_url, status_code=303)


@router.get("/logout")
async def portal_logout_page(
    request: Request,
    session: AsyncSession = Depends(get_db_session),
):
    container = request.app.state.container
    if not container.clerk_auth_service.enabled:
        token = request.cookies.get(container.settings.customer_portal.session_cookie_name)
        if token:
            await container.customer_auth_service.revoke_portal_session(session, raw_token=token)
            await session.commit()
        response = RedirectResponse(url="/app/login", status_code=303)
        response.delete_cookie(container.settings.customer_portal.session_cookie_name)
        _clear_security_confirm_cookie(response)
        return response

    response = _portal_response(
        request,
        "portal/logout.html",
        customer_user=None,
        portal_locked=True,
        clerk_enabled=container.clerk_auth_service.enabled,
        clerk_publishable_key=container.settings.clerk.publishable_key,
        clerk_frontend_api_url=container.settings.clerk.frontend_api_url,
        sign_out_redirect_url="/app/login?signed_out=1",
    )
    response.delete_cookie(container.settings.clerk.backend_session_cookie_name)
    response.delete_cookie(container.settings.clerk.session_cookie_name)
    _clear_security_confirm_cookie(response)
    return response


@router.post("/logout")
async def portal_logout(
    request: Request,
):
    return RedirectResponse(url="/app/logout", status_code=303)


@router.get("/session/callback")
async def portal_clerk_session_callback(
    request: Request,
):
    container = request.app.state.container
    next_path = _safe_resume_path(request.query_params.get("next"))
    return _portal_response(
        request,
        "portal/clerk_callback.html",
        next_path=next_path,
        sign_in_url=_auth_page_url("/app/login", resume_path=next_path, reason="invalid_session"),
        clerk_enabled=container.clerk_auth_service.enabled,
        clerk_publishable_key=container.settings.clerk.publishable_key,
        clerk_frontend_api_url=container.settings.clerk.frontend_api_url,
    )


@router.post("/auth/sync")
async def portal_clerk_auth_sync(
    request: Request,
    session: AsyncSession = Depends(get_db_session),
):
    container = request.app.state.container
    if not container.clerk_auth_service.enabled:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND)
    try:
        payload = await request.json()
    except Exception as exc:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="Invalid request body") from exc
    raw_token = str(payload.get("token") or "").strip()
    hinted_email = str(payload.get("email") or "").strip().lower()
    hinted_display_name = str(payload.get("display_name") or "").strip()
    if not raw_token:
        logger.info("portal_auth_sync_failed", code="missing_token")
        return JSONResponse(
            {
                "ok": False,
                "code": "invalid_session",
                "detail": "Missing Clerk token",
                "login_url": _auth_page_url("/app/login", resume_path=request.query_params.get("resume"), reason="invalid_session"),
                "retryable": False,
            },
            status_code=status.HTTP_400_BAD_REQUEST,
        )
    try:
        claims = container.clerk_auth_service.verify_token(raw_token)
        if hinted_email and "@clerk.local" not in hinted_email:
            claims.email = hinted_email
            claims.raw = {**claims.raw, "email": hinted_email, "email_address": hinted_email}
        if hinted_display_name:
            claims.raw = {
                **claims.raw,
                "name": hinted_display_name,
                "full_name": claims.raw.get("full_name") or hinted_display_name,
            }
        tenant = await container.clerk_auth_service.resolve_tenant_context(session, claims)
        await session.commit()
    except Exception as exc:
        await session.rollback()
        logger.info("portal_auth_sync_failed", code="invalid_session")
        return JSONResponse(
            {
                "ok": False,
                "code": "invalid_session",
                "detail": "Invalid Clerk session",
                "login_url": _auth_page_url("/app/login", reason="invalid_session"),
                "retryable": False,
            },
            status_code=status.HTTP_401_UNAUTHORIZED,
        )

    response = JSONResponse(
        {
            "ok": True,
            "account_id": str(tenant.account.id),
            "clerk_org_id": tenant.clerk_org_id,
            "role": tenant.role.value,
        }
    )
    logger.info(
        "portal_auth_sync_succeeded",
        clerk_user_id=tenant.clerk_user_id,
        clerk_org_id=tenant.clerk_org_id,
        account_id=str(tenant.account.id),
    )
    response.set_cookie(
        container.settings.clerk.backend_session_cookie_name,
        raw_token,
        httponly=True,
        secure=container.settings.customer_portal.secure_cookies or request.url.scheme == "https",
        samesite="lax",
        max_age=container.settings.customer_portal.session_max_age_seconds,
    )
    return response


@router.post("/auth/clear")
async def portal_clerk_auth_clear(
    request: Request,
):
    container = request.app.state.container
    response = JSONResponse({"ok": True})
    response.delete_cookie(container.settings.clerk.backend_session_cookie_name)
    _clear_security_confirm_cookie(response)
    return response


@router.get("/verify")
async def portal_verify_page(
    request: Request,
    session: AsyncSession = Depends(get_db_session),
    context: PortalRequestContext = Depends(require_portal_context),
):
    verification_case = None
    if not context.container.clerk_auth_service.enabled:
        verification_case = await context.container.customer_auth_service.current_verification_case(
            session,
            customer_user=context.customer_user,
        )
    return _portal_response(
        request,
        "portal/verify.html",
        customer_user=context.customer_user,
        verification_case=verification_case,
        csrf_token=context.csrf_token,
        legacy_auth_enabled=not context.container.clerk_auth_service.enabled,
        mfa_verified=context.mfa_verified,
    )


@router.post("/verify/email")
async def portal_verify_email_submit(
    request: Request,
    session: AsyncSession = Depends(get_db_session),
    context: PortalRequestContext = Depends(require_portal_context),
):
    if not context.container.clerk_auth_service.enabled:
        await _verify_portal_csrf(request, context)
        form = await request.form()
        limits = context.container.settings.rate_limit
        ip = _client_ip(request) or "unknown"
        await _enforce_rate_limit(
            request,
            key=f"verify-email:{ip}:{context.customer_user.account_id}",
            limit=limits.verify_email_limit,
            window_seconds=limits.verify_email_window_seconds,
        )
        token = str(form.get("email_token", "")).strip()
        verified = await context.container.customer_auth_service.verify_email_token(session, token=token)
        await session.commit()
        if verified is None:
            return RedirectResponse(url="/app/verify?error=email", status_code=303)
        return RedirectResponse(url="/app/verify?ok=email", status_code=303)

    await session.rollback()
    return RedirectResponse(url="/app/verify?source=clerk", status_code=303)


@router.post("/verify/phone/send")
async def portal_send_phone_otp(
    request: Request,
    session: AsyncSession = Depends(get_db_session),
    context: PortalRequestContext = Depends(require_portal_context),
):
    if not context.container.clerk_auth_service.enabled:
        await _verify_portal_csrf(request, context)
        limits = context.container.settings.rate_limit
        ip = _client_ip(request) or "unknown"
        await _enforce_rate_limit(
            request,
            key=f"otp-send:{ip}:{context.customer_user.account_id}",
            limit=limits.otp_send_limit,
            window_seconds=limits.otp_send_window_seconds,
        )
        if not context.customer_user.phone_number:
            return RedirectResponse(url="/app/verify?error=phone_missing", status_code=303)
        code = await context.container.customer_auth_service.issue_phone_otp(
            session,
            customer_user=context.customer_user,
        )
        await context.container.notification_service.send_verification_sms(
            to_number=context.customer_user.phone_number,
            otp_code=code,
        )
        await session.commit()
        return RedirectResponse(url="/app/verify?ok=phone_sent", status_code=303)

    await session.rollback()
    return RedirectResponse(url="/app/verify?source=clerk", status_code=303)


@router.post("/verify/phone/check")
async def portal_verify_phone_submit(
    request: Request,
    session: AsyncSession = Depends(get_db_session),
    context: PortalRequestContext = Depends(require_portal_context),
):
    if not context.container.clerk_auth_service.enabled:
        await _verify_portal_csrf(request, context)
        form = await request.form()
        limits = context.container.settings.rate_limit
        ip = _client_ip(request) or "unknown"
        await _enforce_rate_limit(
            request,
            key=f"otp-check:{ip}:{context.customer_user.account_id}",
            limit=limits.otp_check_limit,
            window_seconds=limits.otp_check_window_seconds,
        )
        ok = await context.container.customer_auth_service.verify_phone_otp(
            session,
            customer_user=context.customer_user,
            code=str(form.get("otp_code", "")),
        )
        await session.commit()
        if not ok:
            return RedirectResponse(url="/app/verify?error=otp", status_code=303)
        return RedirectResponse(url="/app/verify?ok=otp", status_code=303)

    await session.rollback()
    return RedirectResponse(url="/app/verify?source=clerk", status_code=303)


@router.get("/onboarding")
async def portal_onboarding_page(
    request: Request,
):
    return RedirectResponse(url="/app/initialize", status_code=303)


@router.post("/onboarding")
async def portal_onboarding_submit(
    request: Request,
):
    return RedirectResponse(url="/app/initialize", status_code=303)


@router.get("/initialize")
async def portal_initialize_page(
    request: Request,
    session: AsyncSession = Depends(get_db_session),
    context: PortalRequestContext = Depends(require_portal_context),
):
    init_result = await context.container.portal_initialization_service.load_context(
        session,
        customer_user=context.customer_user,
    )
    subscription = await context.container.billing_service.get_account_subscription(
        session,
        account_id=context.customer_user.account_id,
    )
    server_saved_at = utc_now().isoformat()
    await session.commit()
    checkout_status = request.query_params.get("checkout")
    initialization_payload = {
        **init_result.context.model_dump(),
        "plan_options": context.container.portal_initialization_service.plan_options(),
        "timezone_options": context.container.portal_initialization_service.timezone_options(),
        "save_url": "/app/initialize/save",
        "preview_url": "/app/initialize/preview",
        "billing_url": "/app/initialize/billing/checkout",
        "draft_event_url": "/app/initialize/draft-event",
        "dashboard_url": "/app/dashboard",
        "resume_url": "/app/initialize",
        "account_scope": str(context.customer_user.account_id),
        "server_saved_at": server_saved_at,
        "csrf_token": context.csrf_token,
    }
    return _portal_response(
        request,
        "portal/initialize.html",
        active_nav="initialize",
        customer_user=context.customer_user,
        csrf_token=context.csrf_token,
        initialization=init_result.context,
        initialization_payload=initialization_payload,
        plan_options=context.container.portal_initialization_service.plan_options(),
        timezone_options=context.container.portal_initialization_service.timezone_options(),
        checkout_status=checkout_status,
        billing_already_active=context.container.billing_service.subscription_blocks_new_checkout(subscription),
        portal_locked=not init_result.context.completion_ready,
        body_class="portal-body portal-initialize-body",
    )


@router.post("/initialize/save")
async def portal_initialize_save(
    request: Request,
    session: AsyncSession = Depends(get_db_session),
    context: PortalRequestContext = Depends(require_portal_context),
):
    try:
        payload = await request.json()
    except Exception as exc:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="Invalid request body") from exc
    csrf_token = str(payload.get("csrf_token") or "")
    if not csrf_token or csrf_token != context.csrf_token:
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="Invalid CSRF token")

    step = str(payload.get("step") or "").strip()
    data = payload.get("data")
    if not isinstance(data, dict):
        data = {}
    intent = request.headers.get("x-resona-step-mode", "advance").strip().lower()
    validate_required = intent != "autosave"

    try:
        result = await context.container.portal_initialization_service.save_step(
            session,
            customer_user=context.customer_user,
            step=step,
            data=data,
            validate_required=validate_required,
            advance_step=intent != "autosave",
        )
        server_saved_at = utc_now().isoformat()
        await session.commit()
        return JSONResponse(
            {
                "ok": True,
                "current_step": result.context.current_step,
                "next_step": _initialization_next_step(
                    result.context.current_step,
                    result.context.completed_steps,
                    result.context.step_order,
                ),
                "previous_step": _initialization_previous_step(
                    result.context.current_step,
                    result.context.step_order,
                ),
                "completed_steps": result.context.completed_steps,
                "validation_errors": {},
                "summary": result.context.summary.model_dump(),
                "snapshot": result.context.snapshot,
                "completion_ready": result.context.completion_ready,
                "billing_status": result.context.billing_status,
                "resume_url": "/app/initialize",
                "server_saved_at": server_saved_at,
            }
        )
    except InitializationValidationError as exc:
        result = await context.container.portal_initialization_service.load_context(
            session,
            customer_user=context.customer_user,
        )
        server_saved_at = utc_now().isoformat()
        await session.commit()
        return JSONResponse(
            {
                "ok": False,
                "current_step": step or result.context.current_step,
                "next_step": step or result.context.current_step,
                "previous_step": _initialization_previous_step(
                    step or result.context.current_step,
                    result.context.step_order,
                ),
                "completed_steps": result.context.completed_steps,
                "validation_errors": exc.errors,
                "summary": result.context.summary.model_dump(),
                "snapshot": result.context.snapshot,
                "completion_ready": result.context.completion_ready,
                "billing_status": result.context.billing_status,
                "resume_url": "/app/initialize",
                "server_saved_at": server_saved_at,
            },
            status_code=400,
        )


@router.post("/initialize/preview")
async def portal_initialize_preview(
    request: Request,
    session: AsyncSession = Depends(get_db_session),
    context: PortalRequestContext = Depends(require_portal_context),
):
    try:
        payload = await request.json()
    except Exception as exc:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="Invalid request body") from exc
    csrf_token = str(payload.get("csrf_token") or "")
    if not csrf_token or csrf_token != context.csrf_token:
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="Invalid CSRF token")

    data = payload.get("data")
    if not isinstance(data, dict):
        data = {}
    cached_preview = await context.container.portal_preview_service.get_cached_preference_preview(
        session,
        customer_user=context.customer_user,
        payload=data,
    )
    if cached_preview is not None:
        return JSONResponse({"ok": True, **cached_preview})

    try:
        ip = _client_ip(request) or "unknown"
        rate_limit = context.container.settings.rate_limit
        await _enforce_rate_limit(
            request,
            key=f"initialize-preview:{ip}:{context.customer_user.account_id}",
            limit=rate_limit.initialize_preview_limit,
            window_seconds=rate_limit.initialize_preview_window_seconds,
        )

        preview = await context.container.portal_preview_service.generate_preference_preview(
            session,
            customer_user=context.customer_user,
            payload=data,
        )
        await session.commit()
    except ValueError as exc:
        await session.rollback()
        return JSONResponse({"ok": False, "detail": str(exc)}, status_code=400)

    return JSONResponse({"ok": True, "cached": False, **preview})


@router.post("/initialize/draft-event")
async def portal_initialize_draft_event(
    request: Request,
    context: PortalRequestContext = Depends(require_portal_context),
):
    try:
        payload = await request.json()
    except Exception as exc:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="Invalid request body") from exc

    csrf_token = str(payload.get("csrf_token") or "")
    if not csrf_token or csrf_token != context.csrf_token:
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="Invalid CSRF token")

    event_type = str(payload.get("event_type") or "").strip().lower()
    if event_type not in {"restored", "discarded"}:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="Unknown draft event")

    logger.info(
        "portal_initialize_draft_event",
        event_type=event_type,
        account_id=str(context.customer_user.account_id),
        clerk_user_id=context.clerk_user_id,
    )
    return JSONResponse({"ok": True})


@router.post("/initialize/billing/checkout")
async def portal_initialize_billing_checkout(
    request: Request,
    session: AsyncSession = Depends(get_db_session),
    context: PortalRequestContext = Depends(require_portal_context),
):
    try:
        payload = await request.json()
    except Exception:
        payload = {}
    csrf_token = str(payload.get("csrf_token") or "")
    if not csrf_token or csrf_token != context.csrf_token:
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="Invalid CSRF token")

    init_result = await context.container.portal_initialization_service.load_context(
        session,
        customer_user=context.customer_user,
    )
    account = await session.get(Account, context.customer_user.account_id)
    if account is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND)

    plan_key = str(payload.get("selected_plan_key") or init_result.context.selected_plan_key or "").strip() or None
    if not plan_key:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="Choose a plan before checkout.")
    try:
        plan_result = await context.container.portal_initialization_service.save_step(
            session,
            customer_user=context.customer_user,
            step="plan",
            data={"selected_plan_key": plan_key},
            validate_required=True,
            advance_step=False,
        )
        plan_key = plan_result.context.selected_plan_key or plan_key
    except InitializationValidationError as exc:
        await session.rollback()
        return JSONResponse({"ok": False, "validation_errors": exc.errors}, status_code=400)

    subscription = await context.container.billing_service.get_account_subscription(
        session,
        account_id=context.customer_user.account_id,
    )
    if context.container.billing_service.subscription_blocks_new_checkout(subscription):
        logger.info(
            "portal_duplicate_checkout_prevented",
            account_id=str(context.customer_user.account_id),
            source="initialize",
            status=subscription.status.value if subscription else "unknown",
        )
        await session.commit()
        destination = "/app/dashboard" if plan_result.context.completion_ready else "/app/initialize?checkout=active"
        return JSONResponse({"ok": True, "already_active": True, "url": destination})

    ip = _client_ip(request) or "unknown"
    await _enforce_rate_limit(
        request,
        key=f"initialize-billing:{ip}:{context.customer_user.account_id}",
        limit=max(1, context.container.settings.rate_limit.otp_send_limit),
        window_seconds=context.container.settings.rate_limit.otp_send_window_seconds,
    )

    settings = context.container.settings
    success_url = f"{settings.app.base_url}/app/initialize/return?checkout=success&session_id={{CHECKOUT_SESSION_ID}}"
    cancel_url = f"{settings.app.base_url}/app/initialize/return?checkout=cancel"
    try:
        url = await context.container.billing_service.create_checkout_session(
            session,
            account=account,
            customer_email=context.customer_user.email,
            clerk_org_id=context.clerk_org_id,
            plan_key=plan_key,
            success_url=success_url,
            cancel_url=cancel_url,
        )
    except RuntimeError as exc:
        await session.rollback()
        raise HTTPException(status_code=status.HTTP_503_SERVICE_UNAVAILABLE, detail=str(exc)) from exc
    await session.commit()
    return JSONResponse({"ok": True, "url": url})


@router.get("/initialize/return")
async def portal_initialize_return(
    request: Request,
    session: AsyncSession = Depends(get_db_session),
    context: PortalRequestContext = Depends(require_portal_context),
):
    checkout_status = str(request.query_params.get("checkout") or "").strip() or "cancel"
    session_id = str(request.query_params.get("session_id") or "").strip()
    if checkout_status == "success" and session_id and context.container.billing_service.available:
        try:
            await context.container.billing_service.sync_checkout_session(
                session,
                checkout_session_id=session_id,
            )
        except RuntimeError:
            await session.rollback()
            return RedirectResponse(url="/app/initialize?checkout=processing", status_code=303)

    init_result = await context.container.portal_initialization_service.load_context(
        session,
        customer_user=context.customer_user,
    )
    await session.commit()
    if init_result.context.completion_ready:
        return RedirectResponse(url="/app/initialize?checkout=success", status_code=303)
    return RedirectResponse(url=f"/app/initialize?checkout={checkout_status}", status_code=303)


@router.get("/dashboard")
async def portal_dashboard(
    request: Request,
    session: AsyncSession = Depends(get_db_session),
    context: PortalRequestContext = Depends(require_portal_context),
):
    account = await session.get(Account, context.customer_user.account_id)
    household = await session.scalar(select(Household).where(Household.account_id == context.customer_user.account_id))
    child = await session.scalar(select(ChildProfile).where(ChildProfile.account_id == context.customer_user.account_id))
    subscription = await context.container.billing_service.get_account_subscription(
        session,
        account_id=context.customer_user.account_id,
    )
    usage_summary = await context.container.billing_service.usage_credit_summary(
        session,
        account_id=context.customer_user.account_id,
        subscription=subscription,
    )
    parent_context = ParentDashboardContext(
        household_name=household.name if household else "Household",
        child_name=child.display_name if child and child.display_name else (child.first_name if child else "Not set"),
        subscription_status=subscription.status.value if subscription else "incomplete",
        usage_credit_summary=usage_summary,
    )

    messages = []
    memory_items = []
    safety_events = []
    if child and child.companion_user_id:
        messages = list(
            (
                await session.execute(
                    select(Message)
                    .where(Message.user_id == child.companion_user_id)
                    .order_by(desc(Message.created_at))
                    .limit(8)
                )
            )
            .scalars()
            .all()
        )
        memory_items = list(
            (
                await session.execute(
                    select(MemoryItem)
                    .where(MemoryItem.user_id == child.companion_user_id)
                    .order_by(desc(MemoryItem.updated_at))
                    .limit(6)
                )
            )
            .scalars()
            .all()
        )
        safety_events = list(
            (
                await session.execute(
                    select(SafetyEvent)
                    .where(SafetyEvent.user_id == child.companion_user_id)
                    .order_by(desc(SafetyEvent.created_at))
                    .limit(6)
                )
            )
            .scalars()
            .all()
        )

    return _portal_response(
        request,
        "portal/dashboard.html",
        customer_user=context.customer_user,
        account=account,
        household=household,
        child=child,
        subscription=subscription,
        usage_summary=usage_summary,
        parent_context=parent_context,
        mfa_verified=context.mfa_verified,
        role=context.role.value,
        messages=messages,
        memory_items=memory_items,
        safety_events=safety_events,
    )


@router.get("/security")
async def portal_security_page(
    request: Request,
    session: AsyncSession = Depends(get_db_session),
    context: PortalRequestContext = Depends(require_portal_context),
):
    if context.container.clerk_auth_service.enabled:
        unlocked = _security_gate_unlocked(request, context)
        response = _portal_response(
            request,
            "portal/security.html",
            customer_user=context.customer_user,
            legacy_auth_enabled=False,
            mfa_verified=context.mfa_verified,
            clerk_sign_out_url=context.container.settings.clerk.sign_out_url or "/app/logout",
            security_unlocked=unlocked,
            security_unlock_minutes=max(_SECURITY_CONFIRM_MAX_AGE_SECONDS // 60, 1),
            csrf_token=context.csrf_token,
        )
        if not unlocked:
            _clear_security_confirm_cookie(response)
        return response

    sessions = await context.container.customer_auth_service.active_sessions(
        session,
        customer_user=context.customer_user,
    )
    return _portal_response(
        request,
        "portal/security.html",
        customer_user=context.customer_user,
        legacy_auth_enabled=True,
        sessions=sessions,
        csrf_token=context.csrf_token,
    )


@router.post("/security/confirm")
async def portal_security_confirm(
    request: Request,
    session: AsyncSession = Depends(get_db_session),
    context: PortalRequestContext = Depends(require_portal_context),
):
    if not context.container.clerk_auth_service.enabled:
        await session.rollback()
        return RedirectResponse(url="/app/security", status_code=303)

    await _verify_portal_csrf(request, context)
    form = await request.form()
    password = str(form.get("password") or "")

    error_message: str | None = None
    if not password.strip():
        error_message = "Enter your current password to continue into account security."
    else:
        try:
            verified = await context.container.clerk_auth_service.verify_current_password(
                clerk_user_id=context.clerk_user_id,
                password=password,
            )
        except RuntimeError:
            verified = False
            error_message = "We couldn't confirm your password with Clerk right now. Please try again in a moment."
        if not verified and error_message is None:
            error_message = "That password didn't match your current Clerk password."

    if error_message:
        await session.rollback()
        response = _portal_response(
            request,
            "portal/security.html",
            customer_user=context.customer_user,
            legacy_auth_enabled=False,
            mfa_verified=context.mfa_verified,
            clerk_sign_out_url=context.container.settings.clerk.sign_out_url or "/app/logout",
            security_unlocked=False,
            security_unlock_minutes=max(_SECURITY_CONFIRM_MAX_AGE_SECONDS // 60, 1),
            security_unlock_error=error_message,
            csrf_token=context.csrf_token,
            status_code=status.HTTP_400_BAD_REQUEST,
        )
        _clear_security_confirm_cookie(response)
        return response

    await session.rollback()
    response = RedirectResponse(url="/app/security?confirmed=1", status_code=303)
    response.set_cookie(
        _SECURITY_CONFIRM_COOKIE,
        _create_security_confirm_token(context),
        httponly=True,
        secure=context.container.settings.customer_portal.secure_cookies or request.url.scheme == "https",
        samesite="lax",
        max_age=_SECURITY_CONFIRM_MAX_AGE_SECONDS,
        path="/app",
    )
    return response


@router.post("/security/lock")
async def portal_security_lock(
    request: Request,
    session: AsyncSession = Depends(get_db_session),
    context: PortalRequestContext = Depends(require_portal_context),
):
    if context.container.clerk_auth_service.enabled:
        await _verify_portal_csrf(request, context)
        await session.rollback()
        response = RedirectResponse(url="/app/security", status_code=303)
        _clear_security_confirm_cookie(response)
        return response
    await session.rollback()
    return RedirectResponse(url="/app/security", status_code=303)


@router.post("/security/revoke/{portal_session_id}")
async def portal_revoke_session(
    portal_session_id: str,
    request: Request,
    session: AsyncSession = Depends(get_db_session),
    context: PortalRequestContext = Depends(require_portal_context),
):
    if context.container.clerk_auth_service.enabled:
        await session.rollback()
        return RedirectResponse(url="/app/security", status_code=303)
    await _verify_portal_csrf(request, context)
    await context.container.customer_auth_service.revoke_session_by_id(
        session,
        customer_user=context.customer_user,
        portal_session_id=portal_session_id,
    )
    await session.commit()
    return RedirectResponse(url="/app/security", status_code=303)


@router.get("/billing")
async def portal_billing_page(
    request: Request,
    session: AsyncSession = Depends(get_db_session),
    context: PortalRequestContext = Depends(require_portal_context),
):
    account = await session.get(Account, context.customer_user.account_id)
    subscription = await context.container.billing_service.get_account_subscription(
        session,
        account_id=context.customer_user.account_id,
    )
    usage_summary = await context.container.billing_service.usage_credit_summary(
        session,
        account_id=context.customer_user.account_id,
        subscription=subscription,
    )
    init_result = await context.container.portal_initialization_service.load_context(
        session,
        customer_user=context.customer_user,
    )
    return _portal_response(
        request,
        "portal/billing.html",
        customer_user=context.customer_user,
        account=account,
        subscription=subscription,
        usage_summary=usage_summary,
        stripe_enabled=context.container.billing_service.available,
        billing_already_active=context.container.billing_service.subscription_blocks_new_checkout(subscription),
        csrf_token=context.csrf_token,
        selected_plan_key=init_result.context.selected_plan_key or "chat",
    )


@router.post("/billing/checkout")
async def portal_billing_checkout(
    request: Request,
    session: AsyncSession = Depends(get_db_session),
    context: PortalRequestContext = Depends(require_owner_mfa_context),
):
    await _verify_portal_csrf(request, context)
    form = await request.form()
    ip = _client_ip(request) or "unknown"
    await _enforce_rate_limit(
        request,
        key=f"billing-checkout:{ip}:{context.clerk_user_id}:{context.clerk_org_id}",
        limit=max(1, context.container.settings.rate_limit.otp_send_limit),
        window_seconds=context.container.settings.rate_limit.otp_send_window_seconds,
    )
    account = await session.get(Account, context.customer_user.account_id)
    if account is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND)
    settings = context.container.settings
    success_url = f"{settings.app.base_url}{settings.stripe.success_path}"
    cancel_url = f"{settings.app.base_url}{settings.stripe.cancel_path}"
    subscription = await context.container.billing_service.get_account_subscription(
        session,
        account_id=context.customer_user.account_id,
    )
    if context.container.billing_service.subscription_blocks_new_checkout(subscription):
        logger.info(
            "portal_duplicate_checkout_prevented",
            account_id=str(context.customer_user.account_id),
            source="billing",
            status=subscription.status.value if subscription else "unknown",
        )
        await session.commit()
        return RedirectResponse(url="/app/billing?already_active=1", status_code=303)
    init_result = await context.container.portal_initialization_service.load_context(
        session,
        customer_user=context.customer_user,
    )
    plan_key = (
        str(form.get("plan_key") or "").strip()
        or init_result.context.selected_plan_key
        or context.container.billing_service.plan_key_for_subscription(subscription)
        or "chat"
    )
    try:
        url = await context.container.billing_service.create_checkout_session(
            session,
            account=account,
            customer_email=context.customer_user.email,
            clerk_org_id=context.clerk_org_id,
            plan_key=plan_key,
            success_url=success_url,
            cancel_url=cancel_url,
        )
    except RuntimeError:
        await session.commit()
        return RedirectResponse(url="/app/billing?error=stripe", status_code=303)
    await session.commit()
    return RedirectResponse(url=url, status_code=303)


@router.post("/billing/webhook")
async def stripe_webhook(
    request: Request,
    session: AsyncSession = Depends(get_db_session),
):
    container = request.app.state.container
    payload = await request.body()
    signature = request.headers.get("stripe-signature")
    try:
        result = await container.billing_service.handle_webhook(session, payload=payload, sig_header=signature)
    except RuntimeError as exc:
        raise HTTPException(status_code=503, detail=str(exc)) from exc
    except Exception as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    await session.commit()
    return result


@router.get("/child")
async def portal_child_profile(
    request: Request,
    session: AsyncSession = Depends(get_db_session),
    context: PortalRequestContext = Depends(require_portal_context),
):
    child = await session.scalar(select(ChildProfile).where(ChildProfile.account_id == context.customer_user.account_id))
    child_profile_sections = _child_profile_sections(child) if child else []
    child_overview = None
    if child is not None:
        child_overview = {
            "name": child.display_name or child.first_name or "Not set",
            "birth_year": str(child.birth_year) if child.birth_year else "Not set",
            "companion_status": "Connected" if child.companion_user_id else "Not connected yet",
            "notes": (child.notes or "").strip() or "No extra notes added yet.",
        }
    return _portal_response(
        request,
        "portal/child.html",
        customer_user=context.customer_user,
        child=child,
        child_overview=child_overview,
        child_profile_sections=child_profile_sections,
    )


@router.get("/timeline")
async def portal_timeline(
    request: Request,
    session: AsyncSession = Depends(get_db_session),
    context: PortalRequestContext = Depends(require_portal_context),
):
    child = await session.scalar(select(ChildProfile).where(ChildProfile.account_id == context.customer_user.account_id))
    timeline = []
    if child and child.companion_user_id:
        timeline = list(
            (
                await session.execute(
                    select(Message)
                    .where(Message.user_id == child.companion_user_id)
                    .order_by(desc(Message.created_at))
                    .limit(40)
                )
            )
            .scalars()
            .all()
        )
    return _portal_response(request, "portal/timeline.html", customer_user=context.customer_user, child=child, timeline=timeline)


async def _render_portal_memories_page(
    request: Request,
    session: AsyncSession,
    context: PortalRequestContext,
    *,
    view: str,
):
    child = await _portal_child_profile(session, account_id=context.customer_user.account_id)
    memory_user_id = child.companion_user_id if child else None
    query_params = request.query_params
    include_archived = str(query_params.get("archived") or "").strip().lower() in {"1", "true", "yes", "on"}
    search = str(query_params.get("q") or "").strip() if view == "library" else ""
    type_filter = str(query_params.get("type") or "").strip() if view == "library" else ""

    memory_items: list[MemoryItem] = []
    if memory_user_id:
        memory_items = await context.container.memory_service.list_memories_for_user(
            session,
            user_id=memory_user_id,
            include_archived=include_archived,
            search=search or None,
            memory_type=type_filter or None,
            limit=120,
        )

    memory_payload = {
        "csrf_token": context.csrf_token,
        "view": view,
        "child_name": child.display_name if child and child.display_name else (child.first_name if child else "your child"),
        "graph_url": "/app/memories/graph-data",
        "detail_base_url": "/app/memories",
        "library_url": "/app/memories/library",
        "map_url": "/app/memories/map",
        "resume_url": request.url.path,
        "show_archived": include_archived,
        "search": search,
        "type_filter": type_filter,
    }
    return _portal_response(
        request,
        "portal/memories.html",
        active_nav="memories-map" if view == "map" else "memories-library",
        customer_user=context.customer_user,
        child=child,
        memory_view=view,
        memory_payload=memory_payload,
        memory_items=[_memory_row_payload(item) for item in memory_items],
        memory_filter_options=_memory_filter_options(),
        memory_total=len(memory_items),
        show_archived=include_archived,
        search_query=search,
        type_filter=type_filter,
    )


@router.get("/memories")
async def portal_memories_root():
    return RedirectResponse(url="/app/memories/map", status_code=303)


@router.get("/memory")
async def portal_memory_compat():
    return RedirectResponse(url="/app/memories/map", status_code=303)


@router.get("/memories/map")
async def portal_memories_map(
    request: Request,
    session: AsyncSession = Depends(get_db_session),
    context: PortalRequestContext = Depends(require_portal_context),
):
    return await _render_portal_memories_page(request, session, context, view="map")


@router.get("/memories/library")
async def portal_memories_library(
    request: Request,
    session: AsyncSession = Depends(get_db_session),
    context: PortalRequestContext = Depends(require_portal_context),
):
    return await _render_portal_memories_page(request, session, context, view="library")


@router.get("/memories/graph-data")
async def portal_memories_graph_data(
    request: Request,
    session: AsyncSession = Depends(get_db_session),
    context: PortalRequestContext = Depends(require_portal_context),
):
    child = await _portal_child_profile(session, account_id=context.customer_user.account_id)
    if child is None or child.companion_user_id is None:
        return JSONResponse(
            {
                "ok": True,
                "nodes": [],
                "structural_edges": [],
                "similarity_edges": [],
            }
        )
    include_archived = str(request.query_params.get("archived") or "").strip().lower() in {"1", "true", "yes", "on"}
    graph = await context.container.memory_service.graph_snapshot(
        session,
        user_id=child.companion_user_id,
        include_archived=include_archived,
    )
    await session.commit()
    return JSONResponse(
        {
            "ok": True,
            "nodes": [node.model_dump(mode="json") for node in graph.nodes],
            "structural_edges": [edge.model_dump(mode="json") for edge in graph.structural_edges],
            "similarity_edges": [edge.model_dump(mode="json") for edge in graph.similarity_edges],
        }
    )


@router.get("/memories/{memory_id}")
async def portal_memory_detail(
    memory_id: str,
    session: AsyncSession = Depends(get_db_session),
    context: PortalRequestContext = Depends(require_portal_context),
):
    child = await _portal_child_profile(session, account_id=context.customer_user.account_id)
    if child is None or child.companion_user_id is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="No memory profile is linked yet.")
    inspector = await context.container.memory_service.memory_inspector(
        session,
        user_id=child.companion_user_id,
        memory_id=memory_id,
    )
    if inspector is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Memory not found.")
    await session.commit()
    return JSONResponse({"ok": True, "memory": inspector.model_dump(mode="json")})


@router.post("/memories/{memory_id}")
async def portal_memory_update(
    memory_id: str,
    request: Request,
    session: AsyncSession = Depends(get_db_session),
    context: PortalRequestContext = Depends(require_portal_context),
):
    child = await _portal_child_profile(session, account_id=context.customer_user.account_id)
    if child is None or child.companion_user_id is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="No memory profile is linked yet.")
    try:
        payload = await request.json()
    except Exception as exc:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="Invalid request body") from exc
    csrf_token = str(payload.get("csrf_token") or "")
    if not csrf_token or csrf_token != context.csrf_token:
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="Invalid CSRF token")

    try:
        inspector = await context.container.memory_service.update_memory_for_parent(
            session,
            user_id=child.companion_user_id,
            memory_id=memory_id,
            data=payload.get("data") if isinstance(payload.get("data"), dict) else {},
            config=await context.container.config_service.get_effective_config(session),
        )
    except ValueError as exc:
        await session.rollback()
        return JSONResponse({"ok": False, "detail": str(exc)}, status_code=400)

    if inspector is None:
        await session.rollback()
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Memory not found.")
    await session.commit()
    return JSONResponse({"ok": True, "memory": inspector.model_dump(mode="json")})


@router.post("/memories/{memory_id}/delete-preview")
async def portal_memory_delete_preview(
    memory_id: str,
    request: Request,
    session: AsyncSession = Depends(get_db_session),
    context: PortalRequestContext = Depends(require_portal_context),
):
    child = await _portal_child_profile(session, account_id=context.customer_user.account_id)
    if child is None or child.companion_user_id is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="No memory profile is linked yet.")
    try:
        payload = await request.json()
    except Exception:
        payload = {}
    csrf_token = str(payload.get("csrf_token") or "")
    if not csrf_token or csrf_token != context.csrf_token:
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="Invalid CSRF token")

    preview = await context.container.memory_service.delete_preview_for_parent(
        session,
        user_id=child.companion_user_id,
        memory_id=memory_id,
    )
    if preview is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Memory not found.")
    await session.commit()
    return JSONResponse({"ok": True, "preview": preview.model_dump(mode="json")})


@router.post("/memories/{memory_id}/delete")
async def portal_memory_delete(
    memory_id: str,
    request: Request,
    session: AsyncSession = Depends(get_db_session),
    context: PortalRequestContext = Depends(require_portal_context),
):
    child = await _portal_child_profile(session, account_id=context.customer_user.account_id)
    if child is None or child.companion_user_id is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="No memory profile is linked yet.")
    try:
        payload = await request.json()
    except Exception:
        payload = {}
    csrf_token = str(payload.get("csrf_token") or "")
    if not csrf_token or csrf_token != context.csrf_token:
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="Invalid CSRF token")

    preview = await context.container.memory_service.delete_memory_for_parent(
        session,
        user_id=child.companion_user_id,
        memory_id=memory_id,
    )
    if preview is None:
        await session.rollback()
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Memory not found.")
    await session.commit()
    return JSONResponse(
        {
            "ok": True,
            "preview": preview.model_dump(mode="json"),
            "deleted_ids": [entry.id for entry in preview.affected],
        }
    )


@router.get("/safety")
async def portal_safety(
    request: Request,
    session: AsyncSession = Depends(get_db_session),
    context: PortalRequestContext = Depends(require_portal_context),
):
    child = await session.scalar(select(ChildProfile).where(ChildProfile.account_id == context.customer_user.account_id))
    events = []
    if child and child.companion_user_id:
        events = list(
            (
                await session.execute(
                    select(SafetyEvent)
                    .where(SafetyEvent.user_id == child.companion_user_id)
                    .order_by(desc(SafetyEvent.created_at))
                    .limit(50)
                )
            )
            .scalars()
            .all()
        )
    return _portal_response(request, "portal/safety.html", customer_user=context.customer_user, child=child, events=events)


@router.get("/team")
async def portal_team(
    request: Request,
    session: AsyncSession = Depends(get_db_session),
    context: PortalRequestContext = Depends(require_owner_mfa_context),
):
    members = list(
        (
            await session.execute(
                select(CustomerUser)
                .where(CustomerUser.account_id == context.customer_user.account_id)
                .order_by(CustomerUser.created_at)
            )
        )
        .scalars()
        .all()
    )
    return _portal_response(request, "portal/team.html", customer_user=context.customer_user, members=members)


async def _verify_portal_csrf(request: Request, context: PortalRequestContext) -> None:
    form = await request.form()
    csrf_token = str(form.get("csrf_token", ""))
    if not csrf_token or csrf_token != context.csrf_token:
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="Invalid CSRF token")


async def _enforce_rate_limit(
    request: Request,
    *,
    key: str,
    limit: int,
    window_seconds: int,
) -> None:
    container = request.app.state.container
    decision = await container.rate_limiter_service.enforce(
        key=key,
        limit=limit,
        window_seconds=window_seconds,
    )
    if decision.allowed:
        return
    raise HTTPException(
        status_code=status.HTTP_429_TOO_MANY_REQUESTS,
        detail=f"Rate limit exceeded. Retry in {decision.retry_after_seconds} seconds.",
        headers={"Retry-After": str(decision.retry_after_seconds)},
    )
