from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException, Request, status
from fastapi.responses import RedirectResponse
from sqlalchemy import desc, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.templating import templates
from app.db.session import get_db_session
from app.models.communication import Message, SafetyEvent
from app.models.memory import MemoryItem
from app.models.portal import Account, ChildProfile, CustomerUser, Household
from app.portal.dependencies import (
    PortalRequestContext,
    get_optional_portal_context,
    require_owner_mfa_context,
    require_portal_context,
)
from app.schemas.site import ParentDashboardContext, PortalNavItem

router = APIRouter(prefix="/app", tags=["portal"])


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
    nav_items = [
        PortalNavItem(href="/app/dashboard", label="Dashboard", key="dashboard"),
        PortalNavItem(href="/app/onboarding", label="Setup", key="onboarding"),
        PortalNavItem(href="/app/child", label="Child Profile", key="child"),
        PortalNavItem(href="/app/timeline", label="Conversation Timeline", key="timeline"),
        PortalNavItem(href="/app/memory", label="Memory Highlights", key="memory"),
        PortalNavItem(href="/app/safety", label="Safety Events", key="safety"),
        PortalNavItem(href="/app/team", label="Co-Guardians", key="team"),
        PortalNavItem(href="/app/billing", label="Billing", key="billing"),
        PortalNavItem(href="/app/security", label="Security", key="security"),
    ]
    payload = {
        "request": request,
        "brand_name": settings.web.brand_name,
        "support_email": settings.web.support_email,
        "privacy_url": settings.web.privacy_url,
        "terms_url": settings.web.terms_url,
        "safety_policy_url": settings.web.safety_policy_url,
        "portal_nav_items": nav_items,
        **context,
    }
    return templates.TemplateResponse(template, payload, status_code=status_code)


@router.get("")
async def portal_root(
    context: PortalRequestContext | None = Depends(get_optional_portal_context),
):
    if context is None:
        return RedirectResponse(url="/app/login", status_code=303)
    return RedirectResponse(url="/app/dashboard", status_code=303)


@router.get("/signup")
async def portal_signup_page(request: Request):
    container = request.app.state.container
    return _portal_response(
        request,
        "portal/signup.html",
        legacy_auth_enabled=not container.clerk_auth_service.enabled,
        clerk_sign_up_url=container.settings.clerk.sign_up_url,
    )


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
    return RedirectResponse(url=container.settings.clerk.sign_up_url, status_code=303)


@router.get("/login")
async def portal_login_page(request: Request):
    container = request.app.state.container
    return _portal_response(
        request,
        "portal/login.html",
        reason=request.query_params.get("reason"),
        legacy_auth_enabled=not container.clerk_auth_service.enabled,
        clerk_sign_in_url=container.settings.clerk.sign_in_url,
        clerk_sign_up_url=container.settings.clerk.sign_up_url,
    )


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
        response = RedirectResponse(url="/app/dashboard", status_code=303)
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


@router.post("/logout")
async def portal_logout(
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
        return response

    response = RedirectResponse(
        url=container.settings.clerk.sign_out_url or "/app/login",
        status_code=303,
    )
    response.delete_cookie(container.settings.clerk.session_cookie_name)
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
    session: AsyncSession = Depends(get_db_session),
    context: PortalRequestContext = Depends(require_portal_context),
):
    household = await session.scalar(select(Household).where(Household.account_id == context.customer_user.account_id))
    child = await session.scalar(select(ChildProfile).where(ChildProfile.account_id == context.customer_user.account_id))
    return _portal_response(
        request,
        "portal/onboarding.html",
        customer_user=context.customer_user,
        household=household,
        child=child,
        csrf_token=context.csrf_token,
    )


@router.post("/onboarding")
async def portal_onboarding_submit(
    request: Request,
    session: AsyncSession = Depends(get_db_session),
    context: PortalRequestContext = Depends(require_portal_context),
):
    await _verify_portal_csrf(request, context)
    ip = _client_ip(request) or "unknown"
    await _enforce_rate_limit(
        request,
        key=f"onboarding:{ip}:{context.clerk_user_id}:{context.clerk_org_id}",
        limit=max(1, context.container.settings.rate_limit.signup_limit),
        window_seconds=context.container.settings.rate_limit.signup_window_seconds,
    )
    form = await request.form()
    try:
        await context.container.customer_auth_service.complete_onboarding(
            session,
            customer_user=context.customer_user,
            mode=str(form.get("mode", "for_someone_else")),
            relationship=str(form.get("relationship", "guardian")),
            household_name=str(form.get("household_name", "")),
            child_name=str(form.get("child_name", "")),
            timezone=str(form.get("timezone", "America/New_York")),
            child_phone_number=str(form.get("child_phone_number", "")).strip() or None,
        )
    except ValueError as exc:
        return _portal_response(
            request,
            "portal/onboarding.html",
            error=str(exc),
            customer_user=context.customer_user,
            csrf_token=context.csrf_token,
            status_code=400,
        )
    await session.commit()
    return RedirectResponse(url="/app/dashboard", status_code=303)


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
    usage_summary = context.container.billing_service.usage_credit_summary(subscription)
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
        if context.role.value != "owner":
            raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="Owner permissions required")
        if context.container.settings.clerk.require_owner_mfa and not context.mfa_verified:
            return RedirectResponse(url="/app/login?reason=mfa_required", status_code=303)
        return _portal_response(
            request,
            "portal/security.html",
            customer_user=context.customer_user,
            legacy_auth_enabled=False,
            mfa_verified=context.mfa_verified,
            clerk_sign_out_url=context.container.settings.clerk.sign_out_url or "/app/logout",
        )

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
    usage_summary = context.container.billing_service.usage_credit_summary(subscription)
    return _portal_response(
        request,
        "portal/billing.html",
        customer_user=context.customer_user,
        account=account,
        subscription=subscription,
        usage_summary=usage_summary,
        stripe_enabled=context.container.billing_service.available,
        csrf_token=context.csrf_token,
    )


@router.post("/billing/checkout")
async def portal_billing_checkout(
    request: Request,
    session: AsyncSession = Depends(get_db_session),
    context: PortalRequestContext = Depends(require_owner_mfa_context),
):
    await _verify_portal_csrf(request, context)
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
    try:
        url = await context.container.billing_service.create_checkout_session(
            session,
            account=account,
            customer_email=context.customer_user.email,
            clerk_org_id=context.clerk_org_id,
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
    return _portal_response(request, "portal/child.html", customer_user=context.customer_user, child=child)


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


@router.get("/memory")
async def portal_memory(
    request: Request,
    session: AsyncSession = Depends(get_db_session),
    context: PortalRequestContext = Depends(require_portal_context),
):
    child = await session.scalar(select(ChildProfile).where(ChildProfile.account_id == context.customer_user.account_id))
    memory_items = []
    if child and child.companion_user_id:
        memory_items = list(
            (
                await session.execute(
                    select(MemoryItem)
                    .where(MemoryItem.user_id == child.companion_user_id)
                    .order_by(desc(MemoryItem.updated_at))
                    .limit(50)
                )
            )
            .scalars()
            .all()
        )
    return _portal_response(request, "portal/memory.html", customer_user=context.customer_user, child=child, memory_items=memory_items)


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
