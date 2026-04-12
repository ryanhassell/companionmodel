from __future__ import annotations

import hmac
import json
import secrets
import uuid
from datetime import datetime
from typing import Any
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit

from fastapi import APIRouter, Depends, HTTPException, Request, status
from fastapi.responses import JSONResponse, RedirectResponse, StreamingResponse
from itsdangerous import BadSignature, URLSafeTimedSerializer
from sqlalchemy import desc, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.ai import AIUnavailableError
from app.core.logging import get_logger
from app.core.settings import RuntimeSettings
from app.core.templating import templates
from app.db.session import get_db_session
from app.models.enums import MemoryEntityKind, MemoryFacet, MemoryType, SafetySeverity, SubscriptionStatus
from app.models.communication import Message, SafetyEvent
from app.models.memory import MemoryItem
from app.models.persona import Persona
from app.models.portal import Account, ChildProfile, CustomerUser, Household, PortalChatThread
from app.models.user import User
from app.portal.dependencies import (
    PortalRequestContext,
    get_optional_portal_context,
    require_owner_mfa_context,
    require_portal_context,
)
from app.schemas.site import (
    DashboardCallout,
    DashboardCalloutAction,
    DashboardConversationPreview,
    DashboardInsightCard,
    DashboardMemoryPreview,
    DashboardSafetyPreview,
    DashboardStatusItem,
    DashboardUsageHero,
    DashboardUsageMetric,
    GuidancePlanCard,
    GuidanceQuestionCard,
    ParentDashboardContext,
    PortalResonaSummaryView,
    PortalNavItem,
    PortalNavSection,
)
from app.services.portal_initialization import InitializationValidationError
from app.services.portal_resona import (
    apply_portal_resona_to_persona,
    build_resona_summary,
    default_preset_key,
    default_voice_profile_key,
    find_voice_profile,
    portal_resona_presets,
    portal_voice_profiles,
    preview_text_for_name,
)
from app.utils.text import truncate_text
from app.utils.time import utc_now

router = APIRouter(prefix="/app", tags=["portal"])
logger = get_logger(__name__)
_SECURITY_CONFIRM_COOKIE = "resona_security_confirmed"
_SECURITY_CONFIRM_SALT = "portal-security-confirmed"
_SECURITY_CONFIRM_MAX_AGE_SECONDS = 600
_PORTAL_SELECTED_CHILD_COOKIE = "resona_selected_child"
_MEMORY_CLEAR_CAPTCHA_SALT = "portal-memory-clear-captcha"
_MEMORY_CLEAR_CAPTCHA_MAX_AGE_SECONDS = 1800


def _security_confirm_serializer(settings: RuntimeSettings) -> URLSafeTimedSerializer:
    return URLSafeTimedSerializer(settings.app.secret_key, salt=_SECURITY_CONFIRM_SALT)


def _memory_clear_captcha_serializer(settings: RuntimeSettings) -> URLSafeTimedSerializer:
    return URLSafeTimedSerializer(settings.app.secret_key, salt=_MEMORY_CLEAR_CAPTCHA_SALT)


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


def _clear_selected_child_cookie(response: RedirectResponse | JSONResponse | object) -> None:
    if hasattr(response, "delete_cookie"):
        response.delete_cookie(_PORTAL_SELECTED_CHILD_COOKIE, path="/app")


def _selected_child_cookie_value(request: Request) -> str:
    query_value = str(request.query_params.get("child_id") or "").strip()
    if query_value:
        return query_value
    return str(request.cookies.get(_PORTAL_SELECTED_CHILD_COOKIE) or "").strip()


def _child_profile_display_name(child: ChildProfile | None) -> str:
    if child is None:
        return "No child linked"
    return (child.display_name or child.first_name or "Child").strip() or "Child"


async def _portal_child_persona(
    session: AsyncSession,
    *,
    child: ChildProfile | None,
) -> Persona | None:
    if child is None:
        return None
    if child.companion_user_id:
        companion_user = await session.get(User, child.companion_user_id)
        if companion_user and companion_user.preferred_persona_id:
            persona = await session.get(Persona, companion_user.preferred_persona_id)
            if persona is not None:
                return persona
    pending_persona_id = str((child.preferences_json or {}).get("pending_persona_id") or "").strip()
    if not pending_persona_id:
        return None
    try:
        return await session.get(Persona, uuid.UUID(pending_persona_id))
    except ValueError:
        return None


async def _portal_child_resona_summary(
    session: AsyncSession,
    *,
    settings: RuntimeSettings,
    child: ChildProfile | None,
) -> PortalResonaSummaryView | None:
    if child is None:
        return None
    persona = await _portal_child_persona(session, child=child)
    return build_resona_summary(settings, persona=persona, snapshot=child.preferences_json or {})


def _child_profile_companion_status(child: ChildProfile) -> str:
    return "Connected" if child.companion_user_id else "Needs companion link"


def _child_profile_birth_label(child: ChildProfile) -> str:
    return str(child.birth_year) if child.birth_year else "Birth year not added"


def _portal_child_switcher_options(children: list[ChildProfile], *, selected_child_id: str) -> list[dict[str, str]]:
    options: list[dict[str, str]] = []
    for child in children:
        options.append(
            {
                "id": str(child.id),
                "label": _child_profile_display_name(child),
                "detail": f"{_child_profile_companion_status(child)} · {_child_profile_birth_label(child)}",
                "active": "true" if str(child.id) == selected_child_id else "false",
            }
        )
    return options


def _active_child_profiles(children: list[ChildProfile]) -> list[ChildProfile]:
    return [child for child in children if child.is_active]


async def _portal_child_scope(
    request: Request,
    session: AsyncSession,
    *,
    account_id,
) -> tuple[list[ChildProfile], ChildProfile | None]:
    children = list(
        (
            await session.execute(
                select(ChildProfile)
                .where(ChildProfile.account_id == account_id)
                .order_by(desc(ChildProfile.is_active), ChildProfile.created_at, ChildProfile.display_name, ChildProfile.first_name)
            )
        )
        .scalars()
        .all()
    )
    active_children = _active_child_profiles(children)
    selected_child_id = _selected_child_cookie_value(request)
    selected_child = next((child for child in active_children if str(child.id) == selected_child_id), None)
    if selected_child is None:
        selected_child = active_children[0] if active_children else None

    resolved_child_id = str(selected_child.id) if selected_child else ""
    request.state.portal_child_options = _portal_child_switcher_options(active_children, selected_child_id=resolved_child_id)
    request.state.portal_selected_child_id = resolved_child_id
    request.state.portal_selected_child_name = _child_profile_display_name(selected_child)
    request.state.portal_child_count = len(active_children)
    request.state.portal_total_child_count = len(children)
    request.state.portal_archived_child_count = max(len(children) - len(active_children), 0)
    request.state.portal_selected_child_query = bool(str(request.query_params.get("child_id") or "").strip())
    return children, selected_child


def _memory_store_confirmation_phrase(child_name: str | None) -> str:
    display_name = str(child_name or "child").strip() or "child"
    return f"delete memory store for {display_name}"


def _normalize_confirmation_value(value: str | None) -> str:
    return " ".join(str(value or "").strip().split()).casefold()


def _memory_clear_captcha(context: PortalRequestContext, *, child_name: str | None) -> dict[str, str]:
    left = secrets.randbelow(7) + 3
    right = secrets.randbelow(8) + 2
    answer = str(left + right)
    payload = {
        "account_id": context.account_id,
        "clerk_user_id": context.clerk_user_id,
        "clerk_org_id": context.clerk_org_id,
        "answer": answer,
        "child_name": str(child_name or "").strip(),
    }
    token = _memory_clear_captcha_serializer(context.container.settings).dumps(payload)
    return {
        "question": f"What is {left} + {right}?",
        "token": token,
    }


def _memory_clear_captcha_valid(
    token: str | None,
    answer: str | None,
    *,
    context: PortalRequestContext,
    child_name: str | None,
) -> bool:
    signed = str(token or "").strip()
    if not signed:
        return False
    try:
        payload = _memory_clear_captcha_serializer(context.container.settings).loads(
            signed,
            max_age=_MEMORY_CLEAR_CAPTCHA_MAX_AGE_SECONDS,
        )
    except BadSignature:
        return False
    expected = {
        "account_id": context.account_id,
        "clerk_user_id": context.clerk_user_id,
        "clerk_org_id": context.clerk_org_id,
        "child_name": str(child_name or "").strip(),
    }
    for key, value in expected.items():
        if not hmac.compare_digest(str(payload.get(key, "")), str(value)):
            return False
    expected_answer = str(payload.get("answer", "")).strip()
    provided = str(answer or "").strip()
    return bool(expected_answer) and hmac.compare_digest(expected_answer, provided)


def _append_query_params(url: str, **params: str | None) -> str:
    parts = urlsplit(url)
    query = dict(parse_qsl(parts.query, keep_blank_values=True))
    for key, value in params.items():
        if value is None:
            query.pop(key, None)
        else:
            query[key] = str(value)
    return urlunsplit((parts.scheme, parts.netloc, parts.path, urlencode(query), parts.fragment))


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
                PortalNavItem(href="/app/parent-chat", label="Parent Chat", key="parent-chat"),
            ],
        ),
        PortalNavSection(
            key="guidance",
            label="Guidance",
            items=[
                PortalNavItem(href="/app/plans", label="Plans", key="plans"),
                PortalNavItem(href="/app/questions", label="Questions", key="questions"),
            ],
        ),
        PortalNavSection(
            key="memories",
            label="Memories",
            items=[
                PortalNavItem(href="/app/memories/map", label="Memory Web", key="memories-map"),
                PortalNavItem(href="/app/memories/daily-routine", label="Daily Routine", key="memories-routine"),
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
        "portal_child_options": context.pop("portal_child_options", getattr(request.state, "portal_child_options", [])),
        "portal_selected_child_id": context.pop("portal_selected_child_id", getattr(request.state, "portal_selected_child_id", "")),
        "portal_selected_child_name": context.pop(
            "portal_selected_child_name",
            getattr(request.state, "portal_selected_child_name", ""),
        ),
        "portal_child_count": context.pop("portal_child_count", getattr(request.state, "portal_child_count", 0)),
        **context,
    }
    payload["customer_display_name"] = _customer_display_name(payload.get("customer_user"))
    response = templates.TemplateResponse(template, payload, status_code=status_code)
    selected_child_id = str(payload.get("portal_selected_child_id") or "").strip()
    if selected_child_id:
        if (
            str(request.cookies.get(_PORTAL_SELECTED_CHILD_COOKIE) or "").strip() != selected_child_id
            or bool(getattr(request.state, "portal_selected_child_query", False))
        ):
            response.set_cookie(
                _PORTAL_SELECTED_CHILD_COOKIE,
                selected_child_id,
                httponly=True,
                secure=settings.customer_portal.secure_cookies or request.url.scheme == "https",
                samesite="lax",
                max_age=settings.customer_portal.session_max_age_seconds,
                path="/app",
            )
    return response


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


def _format_dashboard_currency(value: float | None) -> str:
    amount = float(value or 0.0)
    return f"${amount:,.2f}"


def _format_dashboard_timestamp(value: datetime | None) -> str:
    if value is None:
        return "Recently"
    date_part = value.strftime("%b %d, %Y").replace(" 0", " ")
    time_part = value.strftime("%I:%M %p").lstrip("0")
    return f"{date_part} at {time_part}"


def _dashboard_tone_for_subscription(status: str | None) -> str:
    normalized = str(status or "").strip().lower()
    if normalized in {SubscriptionStatus.active.value, SubscriptionStatus.trialing.value}:
        return "positive"
    if normalized == SubscriptionStatus.past_due.value:
        return "warning"
    if normalized in {SubscriptionStatus.canceled.value, SubscriptionStatus.incomplete.value}:
        return "danger"
    return "neutral"


def _dashboard_tone_for_severity(severity: str | None) -> str:
    normalized = str(severity or "").strip().lower()
    if normalized == SafetySeverity.critical.value:
        return "danger"
    if normalized == SafetySeverity.high.value:
        return "warning"
    if normalized in {SafetySeverity.medium.value, SafetySeverity.low.value}:
        return "neutral"
    return "neutral"


def _dashboard_channel_label(channel: str | None) -> str:
    normalized = str(channel or "").strip().lower()
    if normalized == "sms":
        return "SMS"
    if normalized == "mms":
        return "MMS"
    if normalized == "voice":
        return "Voice"
    return _humanize_choice(normalized)


def _dashboard_direction_label(direction: str | None) -> str:
    normalized = str(direction or "").strip().lower()
    if normalized == "inbound":
        return "Incoming"
    if normalized == "outbound":
        return "Outgoing"
    return _humanize_choice(normalized)


def _dashboard_usage_hero(usage_summary, *, subscription_status: str) -> DashboardUsageHero:
    included = float(usage_summary.included_usd or 0.0)
    used = float(usage_summary.used_usd or 0.0)
    remaining = float(usage_summary.remaining_usd or 0.0)
    pending = float(usage_summary.pending_cost_usd or 0.0)
    progress_raw = int(round((used / included) * 100)) if included > 0 else 0
    progress_percent = max(0, min(progress_raw, 100))
    progress_tone = "positive"
    if used >= included and included > 0:
        progress_tone = "danger"
    elif included > 0 and used >= included * 0.75:
        progress_tone = "warning"

    if remaining > 0:
        summary = f"You still have {_format_dashboard_currency(remaining)} in included credits available this month."
    elif included > 0:
        summary = "Included monthly credits are fully used. Additional activity will continue on metered billing."
    else:
        summary = "Your monthly credit summary will appear here as soon as usage starts coming through."

    metrics = [
        DashboardUsageMetric(label="Included this month", value=_format_dashboard_currency(included), detail="Included monthly credits"),
        DashboardUsageMetric(label="Used so far", value=_format_dashboard_currency(used), detail="Processed activity", tone=progress_tone),
        DashboardUsageMetric(label="Still available", value=_format_dashboard_currency(remaining), detail="Before overage billing starts"),
    ]
    if pending > 0:
        lag_minutes = int(usage_summary.reconciliation_lag_minutes or 0)
        detail = "Recent activity is still settling."
        if lag_minutes > 0:
            detail = f"Recent activity can take about {lag_minutes} minutes to settle."
        metrics.append(
            DashboardUsageMetric(
                label="Pending reconciliation",
                value=_format_dashboard_currency(pending),
                detail=detail,
                tone="warning",
            )
        )

    cta_label = "Manage Billing"
    if str(subscription_status or "").strip().lower() in {SubscriptionStatus.incomplete.value, SubscriptionStatus.canceled.value}:
        cta_label = "Activate Plan"

    return DashboardUsageHero(
        title="Monthly Credit Usage",
        summary=summary,
        metrics=metrics,
        progress_percent=progress_percent,
        progress_tone=progress_tone,
        note=str(usage_summary.overage_note or "").strip() or None,
        primary_cta_label=cta_label,
        primary_cta_href="/app/billing",
    )


def _dashboard_status_items(
    *,
    subscription_status: str,
    child: ChildProfile | None,
    child_count: int,
    role_label: str,
    safety_events: list[SafetyEvent],
) -> list[DashboardStatusItem]:
    child_name = _child_profile_display_name(child)
    if child_count <= 0 or child is None:
        child_value = "No child linked"
        child_detail = "No child profile is linked to this portal yet."
        child_tone = "warning"
    elif child.companion_user_id is None:
        child_value = f"{child_count} profile{'s' if child_count != 1 else ''}"
        child_detail = f"{child_name} is selected, and the companion connection still needs to be finished."
        child_tone = "warning"
    else:
        child_value = f"{child_count} profile{'s' if child_count != 1 else ''}"
        child_detail = f"{child_name} is currently in focus across conversations, memories, and safety tools."
        child_tone = "positive"

    if not safety_events:
        safety_value = "All clear"
        safety_detail = "No recent safety events have been recorded."
        safety_tone = "positive"
    else:
        highest = max(safety_events, key=lambda event: {"low": 0, "medium": 1, "high": 2, "critical": 3}.get(event.severity.value, 0))
        safety_count = len(safety_events)
        safety_value = f"{safety_count} recent event{'s' if safety_count != 1 else ''}"
        safety_detail = f"Highest severity: {_humanize_choice(highest.severity.value)}."
        safety_tone = _dashboard_tone_for_severity(highest.severity.value)

    return [
        DashboardStatusItem(
            label="Subscription",
            value=_humanize_choice(subscription_status),
            detail="Billing and access status for this portal account.",
            tone=_dashboard_tone_for_subscription(subscription_status),
        ),
        DashboardStatusItem(
            label="Child profile",
            value=child_value,
            detail=child_detail,
            tone=child_tone,
        ),
        DashboardStatusItem(
            label="Safety snapshot",
            value=safety_value,
            detail=safety_detail,
            tone=safety_tone,
        ),
        DashboardStatusItem(
            label="Access level",
            value=role_label,
            detail="Your current portal permission level for this household.",
            tone="neutral",
        ),
    ]


def _dashboard_message_preview(message: Message) -> DashboardConversationPreview:
    body = truncate_text(" ".join(str(message.body or "").split()), 180) if str(message.body or "").strip() else "No message text recorded."
    timestamp = message.sent_at or message.created_at
    return DashboardConversationPreview(
        direction_label=_dashboard_direction_label(getattr(message.direction, "value", message.direction)),
        channel_label=_dashboard_channel_label(getattr(message.channel, "value", message.channel)),
        timestamp_label=_format_dashboard_timestamp(timestamp),
        body=body,
        href="/app/timeline",
    )


def _dashboard_memory_preview(item: MemoryItem) -> DashboardMemoryPreview:
    title = (item.title or "").strip() or _humanize_choice(getattr(item.memory_type, "value", "memory"))
    summary = (item.summary or "").strip() or " ".join(str(item.content or "").split()).strip()
    return DashboardMemoryPreview(
        title=title,
        memory_type_label=_humanize_choice(getattr(item.memory_type, "value", "memory")),
        summary=truncate_text(summary or "No memory summary recorded yet.", 180),
        updated_label=_format_dashboard_timestamp(item.updated_at),
        href=f"/app/memories/map?node={item.id}",
    )


def _dashboard_safety_preview(event: SafetyEvent) -> DashboardSafetyPreview:
    detail_source = (event.action_taken or "").strip()
    if not detail_source:
        detail_source = f"Detected by {_humanize_choice(event.detector)}."
    return DashboardSafetyPreview(
        title=_humanize_choice(event.event_type),
        severity_label=_humanize_choice(event.severity.value),
        timestamp_label=_format_dashboard_timestamp(event.created_at),
        detail=truncate_text(detail_source, 160),
        tone=_dashboard_tone_for_severity(event.severity.value),
        href="/app/safety",
    )


def _dashboard_household_summary(
    *,
    household: Household | None,
    child: ChildProfile | None,
    child_count: int,
    subscription_label: str,
) -> str:
    household_name = household.name if household else "This household"
    child_name = _child_profile_display_name(child)
    if child is None or child_count <= 0:
        return f"{household_name} is ready for setup. Finish linking a child profile to unlock the full parent portal experience."
    if child_count == 1:
        return f"{household_name} is currently organized around {child_name}, with billing status marked as {subscription_label.lower()}."
    return (
        f"{household_name} currently supports {child_count} child profiles. "
        f"{child_name} is the active portal focus, and billing status is marked as {subscription_label.lower()}."
    )


def _dashboard_callout(subscription_status: str) -> DashboardCallout | None:
    normalized = str(subscription_status or "").strip().lower()
    if normalized not in {SubscriptionStatus.incomplete.value, SubscriptionStatus.canceled.value}:
        return None
    return DashboardCallout(
        title="Portal access is ready whenever you are",
        body="You can keep refining setup and exploring the portal now. Activate billing when you're ready to enable live messaging and voice activity.",
        tone="warning",
        actions=[
            DashboardCalloutAction(label="Review Setup", href="/app/initialize", kind="ghost"),
            DashboardCalloutAction(label="Activate Plan", href="/app/billing", kind="primary"),
        ],
    )


def _profile_value_list(value: Any) -> list[str]:
    if value is None:
        return []
    raw_items = value if isinstance(value, list) else [value]
    items: list[str] = []
    for raw in raw_items:
        label = _humanize_profile_value(raw)
        if label != "Not set":
            items.append(label)
    return items


def _recent_memory_signal(memory_items: list[MemoryItem]) -> tuple[str | None, str | None]:
    for item in memory_items:
        title = (item.title or "").strip() or _humanize_choice(getattr(item.memory_type, "value", "memory"))
        summary = (item.summary or "").strip() or " ".join(str(item.content or "").split()).strip()
        if title or summary:
            return title or None, truncate_text(summary or "", 180) or None
    return None, None


def _guidance_plan_cards(
    *,
    child: ChildProfile | None,
    memory_items: list[MemoryItem],
    safety_events: list[SafetyEvent],
) -> list[GuidancePlanCard]:
    child_name = _child_profile_display_name(child)
    preferences = child.preferences_json if child else {}
    boundaries = child.boundaries_json if child else {}
    routines = child.routines_json if child else {}
    memory_title, memory_summary = _recent_memory_signal(memory_items)

    pacing = _profile_value_list(preferences.get("preferred_pacing"))
    response_style = _profile_value_list(preferences.get("response_style"))
    communication_notes = truncate_text(" ".join(str(preferences.get("communication_notes") or "").split()), 160)

    connection_parts: list[str] = []
    if memory_title:
        connection_parts.append(
            f"A clear theme is already emerging around {memory_title}. Keep adding detail so Resona knows when it helps and why it matters."
        )
    else:
        connection_parts.append(
            f"Start by naming the moments, people, and activities that reliably help {child_name} feel comfortable, seen, or delighted."
        )
    if pacing:
        connection_parts.append(f"Current pacing leans {', '.join(pacing)}.")
    if response_style:
        connection_parts.append(f"The tone is leaning {', '.join(response_style)}.")
    if preferences.get("voice_enabled"):
        connection_parts.append("Voice is available too, so hearing support out loud can be part of the plan.")
    if communication_notes:
        connection_parts.append(communication_notes)

    routine_parts: list[str] = []
    cadence_label = _humanize_profile_value(routines.get("daily_cadence"))
    quiet_hours_label = _humanize_profile_value(routines.get("quiet_hours"))
    if cadence_label != "Not set":
        routine_parts.append(f"{cadence_label} cadence")
    if quiet_hours_label != "Not set":
        routine_parts.append(f"quiet hours at {quiet_hours_label}")
    if routine_parts:
        routine_summary = (
            f"The current routine picture already points to {', '.join(routine_parts)}. "
            f"Now the helpful next step is describing what a smooth day actually feels like for {child_name}."
        )
    else:
        routine_summary = (
            f"Resona will feel much steadier if the household rhythm is explicit. "
            f"Describe the shape of mornings, evenings, transitions, and recovery moments for {child_name}."
        )

    visibility_label = _humanize_profile_value(boundaries.get("parent_visibility_mode"))
    alert_label = _humanize_profile_value(boundaries.get("alert_threshold"))
    if safety_events:
        highest = max(safety_events, key=lambda event: {"low": 0, "medium": 1, "high": 2, "critical": 3}.get(event.severity.value, 0))
        coordination_summary = (
            f"There have been {len(safety_events)} recent safety event{'s' if len(safety_events) != 1 else ''}, "
            f"with the highest severity at {_humanize_choice(highest.severity.value)}. "
            f"This is a good time to tighten expectations for what should trigger parent awareness."
        )
    elif visibility_label != "Not set" or alert_label != "Not set":
        coordination_bits: list[str] = []
        if visibility_label != "Not set":
            coordination_bits.append(f"visibility is set to {visibility_label}")
        if alert_label != "Not set":
            coordination_bits.append(f"alert sensitivity is {alert_label.lower()}")
        coordination_summary = (
            f"The household already has some guardrails in place: {', '.join(coordination_bits)}. "
            f"Use this plan to spell out what should always be surfaced, what can stay lightweight, and what Resona should avoid."
        )
    else:
        coordination_summary = (
            f"Before the household gets busier, it helps to define how involved you want the portal to be, "
            f"what should escalate, and what should simply be handled gently in the background."
        )

    return [
        GuidancePlanCard(
            title="Reinforce what already helps",
            summary=" ".join(connection_parts),
            steps=[
                f"Name the people, topics, and comforts that most reliably help {child_name}.",
                "Add when those things work best so support feels timely instead of generic.",
                "Correct anything that feels outdated so the memory picture keeps up with real life.",
            ],
            href="/app/parent-chat",
            action_label="Refine in Parent Chat",
            tone="positive",
        ),
        GuidancePlanCard(
            title="Turn support into a repeatable rhythm",
            summary=routine_summary,
            steps=[
                f"Describe what a good morning, evening, or transition looks like for {child_name}.",
                "Clarify quiet hours, pacing, and when check-ins feel supportive versus intrusive.",
                "Add one or two routines you want Resona to consistently reinforce over time.",
            ],
            href="/app/child",
            action_label="Review Child Profile",
            tone="neutral",
        ),
        GuidancePlanCard(
            title="Keep the household aligned",
            summary=coordination_summary,
            steps=[
                "Decide what should always be surfaced to a parent and what can stay in the background.",
                "Spell out what should trigger a closer look, follow-up, or direct outreach.",
                "Add anything Resona should avoid so support feels safe, respectful, and consistent.",
            ],
            href="/app/child" if not safety_events else "/app/safety",
            action_label="Review Settings" if not safety_events else "Open Safety Feed",
            tone="warning" if safety_events else "neutral",
        ),
    ]


def _guidance_question_cards(
    *,
    child: ChildProfile | None,
    memory_items: list[MemoryItem],
    safety_events: list[SafetyEvent],
) -> list[GuidanceQuestionCard]:
    child_name = _child_profile_display_name(child)
    preferences = child.preferences_json if child else {}
    boundaries = child.boundaries_json if child else {}
    routines = child.routines_json if child else {}
    notes = truncate_text(" ".join(str(child.notes or "").split()), 180) if child else ""
    questions: list[GuidanceQuestionCard] = []

    def add_question(
        key: str,
        question: str,
        why_it_matters: str,
        suggested_prompt: str,
        *,
        tone: str = "neutral",
    ) -> None:
        if any(card.key == key or card.question == question for card in questions):
            return
        questions.append(
            GuidanceQuestionCard(
                key=key,
                question=question,
                why_it_matters=why_it_matters,
                suggested_prompt=suggested_prompt,
                href="/app/parent-chat",
                action_label="Answer in Parent Chat",
                tone=tone,
            )
        )

    if len(memory_items) < 3:
        add_question(
            "important-world",
            f"Who and what matter most in {child_name}'s world right now?",
            "Naming the important people, comforts, favorites, and everyday anchors gives Resona a much more grounded picture to work from.",
            f"The people and things that matter most to {child_name} right now are...",
            tone="positive",
        )
    if not _profile_value_list(preferences.get("preferred_pacing")) or not _profile_value_list(preferences.get("response_style")):
        add_question(
            "understood-quickly",
            f"What helps {child_name} feel understood quickly?",
            "A small amount of tone and pacing guidance can change whether support feels warm, calming, and familiar versus slightly off.",
            f"When {child_name} needs comfort, it helps when the response feels...",
        )
    if _humanize_profile_value(routines.get("daily_cadence")) == "Not set" and _humanize_profile_value(routines.get("quiet_hours")) == "Not set":
        add_question(
            "transitions-shape",
            f"What should mornings, evenings, or transitions feel like for {child_name}?",
            "Routine works best when Resona understands the emotional shape of the day, not just the calendar.",
            f"A smooth morning or evening for {child_name} usually looks like...",
        )
    if _humanize_profile_value(boundaries.get("parent_visibility_mode")) == "Not set" or _humanize_profile_value(boundaries.get("alert_threshold")) == "Not set":
        add_question(
            "parent-visibility",
            "How hands-on do you want parent visibility to be?",
            "Clear visibility expectations help the portal avoid both under-sharing and over-sharing.",
            "I would like updates when...",
        )
    if not notes:
        add_question(
            "what-backfires",
            f"What backfires for {child_name}, even when people mean well?",
            "Knowing what misses the mark is just as important as knowing what helps.",
            f"What tends not to work for {child_name} is...",
        )
    if memory_items:
        add_question(
            "easy-to-miss",
            f"What feels easy to miss, but matters a lot, in {child_name}'s world lately?",
            "Small meaning-rich details often make the difference between Resona sounding generic and actually understanding why something matters.",
            f"One thing people might miss, but that matters a lot to {child_name}, is...",
        )
    if safety_events:
        add_question(
            "concerning-followup",
            "What should happen after something concerning comes up?",
            "Follow-up feels much more supportive when the household has named what good next steps look like.",
            "If something concerning comes up, the best next step is...",
            tone="warning",
        )

    add_question(
        "recent-change",
        f"What has changed recently for {child_name}?",
        "Fresh context keeps Resona aligned with real life instead of leaning too hard on older patterns.",
        f"Lately, one important change in {child_name}'s world has been...",
    )

    return questions[:4]


def _dashboard_insight_cards(
    *,
    child: ChildProfile | None,
    memory_items: list[MemoryItem],
    plan_cards: list[GuidancePlanCard],
    question_cards: list[GuidanceQuestionCard],
) -> list[DashboardInsightCard]:
    child_name = _child_profile_display_name(child)
    memory_title, memory_summary = _recent_memory_signal(memory_items)
    cards: list[DashboardInsightCard] = []

    if plan_cards:
        cards.append(
            DashboardInsightCard(
                label="Plan",
                title=plan_cards[0].title,
                summary=truncate_text(plan_cards[0].summary, 170),
                href="/app/plans",
                action_label="Open Plans",
                tone=plan_cards[0].tone,
            )
        )
    if question_cards:
        cards.append(
            DashboardInsightCard(
                label="Question",
                title=question_cards[0].question,
                summary=truncate_text(question_cards[0].why_it_matters, 170),
                href=_append_query_params("/app/questions", question=question_cards[0].key),
                action_label="Open Questions",
                tone=question_cards[0].tone,
            )
        )
    if memory_title:
        cards.append(
            DashboardInsightCard(
                label="Memory",
                title=f"{child_name}'s world is becoming more concrete",
                summary=truncate_text(
                    f"Recent memory themes include {memory_title}. {memory_summary or 'Open memories to review what Resona is actively holding onto.'}",
                    170,
                ),
                href="/app/memories/map",
                action_label="Open Memories",
                tone="positive",
            )
        )
    else:
        cards.append(
            DashboardInsightCard(
                label="Memory",
                title="The memory picture still has room to grow",
                summary=(
                    "A few more parent details about favorites, relationships, routines, or hard moments will make the long-term picture much more useful."
                ),
                href="/app/parent-chat",
                action_label="Open Parent Chat",
                tone="neutral",
            )
        )
    return cards[:3]


def _child_profile_sections(child: ChildProfile) -> list[dict[str, Any]]:
    preferences = child.preferences_json or {}
    boundaries = child.boundaries_json or {}
    routines = child.routines_json or {}

    sections: list[dict[str, Any]] = [
        {
            "title": "Communication Preferences",
            "summary": "How Resona should pace itself, sound, and show up for everyday conversations.",
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
            "summary": "The guardrails, visibility rules, and safety thresholds currently guiding the account.",
            "items": [
                {"label": "Proactive check-ins", "value": _humanize_profile_value(boundaries.get("proactive_check_ins"))},
                {"label": "Parent visibility", "value": _humanize_profile_value(boundaries.get("parent_visibility_mode"))},
                {"label": "Alert threshold", "value": _humanize_profile_value(boundaries.get("alert_threshold"))},
            ],
        },
        {
            "title": "Routines",
            "summary": "Recurring cadence and quiet-hour structure used to keep support steady and predictable.",
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

async def _portal_child_profile(request: Request, session: AsyncSession, *, account_id) -> ChildProfile | None:
    _, selected_child = await _portal_child_scope(request, session, account_id=account_id)
    return selected_child


async def _portal_child_activity_snapshot(
    session: AsyncSession,
    *,
    child: ChildProfile | None,
    message_limit: int = 4,
    memory_limit: int = 4,
    safety_limit: int = 4,
) -> tuple[list[Message], list[MemoryItem], list[SafetyEvent]]:
    if child is None or child.companion_user_id is None:
        return [], [], []

    messages = list(
        (
            await session.execute(
                select(Message)
                .where(Message.user_id == child.companion_user_id)
                .order_by(desc(Message.created_at))
                .limit(message_limit)
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
                .limit(memory_limit)
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
                .limit(safety_limit)
            )
        )
        .scalars()
        .all()
    )
    return messages, memory_items, safety_events


def _household_profile_note(child_count: int, *, add_on_label: str | None = None) -> str:
    if child_count <= 0:
        return "No child profile is linked yet. Add the first profile here when you're ready."
    if child_count == 1:
        return "One child profile is currently active for this household, with one active Resona included for that child."
    if add_on_label:
        return f"{child_count} child profiles are currently active. Each profile beyond the first carries a {add_on_label} monthly add-on, and each active child keeps one active Resona."
    return f"{child_count} child profiles are currently active in this household."


def _child_profile_return_url(request: Request, child_id: str | None = None) -> str:
    current = request.url.path
    if request.url.query:
        current = f"{current}?{request.url.query}"
    current = _append_query_params(
        current,
        updated=None,
        added=None,
        archived=None,
        restored=None,
        removed=None,
        error=None,
    )
    if child_id:
        return _append_query_params(current, child_id=child_id)
    return current


def _child_notice_payload(request: Request) -> dict[str, str] | None:
    if str(request.query_params.get("added") or "").strip() == "1":
        return {
            "tone": "success",
            "title": "Child profile added",
            "message": "The new profile has been added to this household and is ready for personalization.",
        }
    if str(request.query_params.get("updated") or "").strip() == "1":
        return {
            "tone": "success",
            "title": "Profile updated",
            "message": "The selected child profile details were saved.",
        }
    if str(request.query_params.get("resona") or "").strip() == "1":
        return {
            "tone": "success",
            "title": "Resona updated",
            "message": "The selected child now has an updated Resona profile, voice, and guidance setup.",
        }
    if str(request.query_params.get("archived") or "").strip() == "1":
        return {
            "tone": "success",
            "title": "Profile archived",
            "message": "That profile has been moved out of the active household roster and billing was updated if needed.",
        }
    if str(request.query_params.get("restored") or "").strip() == "1":
        return {
            "tone": "success",
            "title": "Profile restored",
            "message": "That profile is active in the household again and billing was updated if needed.",
        }
    if str(request.query_params.get("removed") or "").strip() == "1":
        return {
            "tone": "success",
            "title": "Profile removed",
            "message": "The profile was permanently removed from this household.",
        }
    child_error = str(request.query_params.get("error") or "").strip().lower()
    if child_error:
        messages = {
            "setup": "Finish household setup first so the new profile has somewhere to live.",
            "name": "Add at least a first name so we know who this profile belongs to.",
            "birth_year": "Birth year must be a valid four-digit year.",
            "billing": "We couldn't align billing for that profile change just now, so nothing changed.",
            "not_found": "That child profile could not be found for this household.",
            "remove_blocked": "That profile can't be permanently removed because it is already linked to live history or a companion connection. Archive it instead.",
        }
        return {
            "tone": "danger",
            "title": "Profile change not applied",
            "message": messages.get(child_error, "We couldn't update that child profile right now."),
        }
    return None


async def _owned_child_profile(session: AsyncSession, *, account_id, child_id: str) -> ChildProfile | None:
    try:
        child_uuid = uuid.UUID(str(child_id))
    except (TypeError, ValueError):
        return None
    return await session.scalar(
        select(ChildProfile).where(
            ChildProfile.account_id == account_id,
            ChildProfile.id == child_uuid,
        )
    )


async def _child_profile_can_remove(session: AsyncSession, *, child: ChildProfile) -> bool:
    if child.companion_user_id is not None:
        return False
    thread = await session.scalar(
        select(PortalChatThread.id).where(PortalChatThread.child_profile_id == child.id).limit(1)
    )
    return thread is None


def _child_form_payload(
    child: ChildProfile | None,
    *,
    request: Request,
    csrf_token: str,
    can_remove: bool,
) -> dict[str, Any] | None:
    if child is None:
        return None
    return {
        "update_url": f"/app/child/{child.id}/update",
        "archive_url": f"/app/child/{child.id}/archive",
        "remove_url": f"/app/child/{child.id}/remove",
        "csrf_token": csrf_token,
        "next": _child_profile_return_url(request, str(child.id)),
        "first_name": child.first_name or "",
        "display_name": child.display_name or "",
        "birth_year": str(child.birth_year) if child.birth_year else "",
        "notes": child.notes or "",
        "can_remove": can_remove,
    }


async def _child_resona_form_payload(
    session: AsyncSession,
    *,
    settings: RuntimeSettings,
    child: ChildProfile | None,
    csrf_token: str,
) -> dict[str, Any] | None:
    if child is None:
        return None
    persona = await _portal_child_persona(session, child=child)
    preferences = dict(child.preferences_json or {})
    stored = preferences.get("resona_profile", {}) if isinstance(preferences.get("resona_profile"), dict) else {}
    voice_profiles = [item.model_dump(mode="json") for item in portal_voice_profiles(settings)]
    presets = [item.model_dump(mode="json") for item in portal_resona_presets(settings)]
    summary = build_resona_summary(settings, persona=persona, snapshot=stored)
    return {
        "action_url": f"/app/child/{child.id}/resona",
        "preview_url": f"/app/child/{child.id}/resona/preview",
        "csrf_token": csrf_token,
        "resona_summary": summary.model_dump(mode="json"),
        "preset_options": presets,
        "voice_options": voice_profiles,
        "resona_mode": str(stored.get("mode") or summary.mode or "preset"),
        "resona_preset_key": str(stored.get("preset_key") or summary.preset_key or default_preset_key(settings)),
        "resona_display_name": str(summary.display_name or ""),
        "resona_voice_profile_key": str(
            stored.get("voice_profile_key") or summary.voice_profile_key or default_voice_profile_key(settings)
        ),
        "resona_vibe": str(stored.get("vibe") or (persona.tone if persona else "") or ""),
        "resona_support_style": str(stored.get("support_style") or (persona.speech_style if persona else "") or ""),
        "resona_avoid": str(stored.get("avoid") or (persona.boundaries if persona else "") or ""),
        "resona_anchors": str(
            stored.get("anchors")
            or (
                ", ".join(list(persona.topics_of_interest or []) + list(persona.favorite_activities or []))
                if persona
                else ""
            )
        ),
        "resona_proactive_style": str(
            stored.get("proactive_style") or (persona.proactive_outreach_style if persona else "") or ""
        ),
        "resona_description": str(persona.description if persona else "") or "",
        "resona_style": str(persona.style if persona else "") or "",
        "resona_tone": str(persona.tone if persona else "") or "",
        "resona_boundaries": str(persona.boundaries if persona else "") or "",
        "resona_topics": ", ".join(persona.topics_of_interest or []) if persona else "",
        "resona_activities": ", ".join(persona.favorite_activities or []) if persona else "",
        "resona_speech_style": str(persona.speech_style if persona else "") or "",
        "resona_disclosure_style": str(persona.disclosure_policy if persona else "") or "",
        "resona_texting_length": str(persona.texting_length_preference if persona else "") or "",
        "resona_emoji_tendency": str(persona.emoji_tendency if persona else "") or "",
        "resona_parent_notes": str(persona.operator_notes if persona else "") or "",
        "status": "connected" if child.companion_user_id else "pending",
    }


def _archived_child_cards(request: Request, *, children: list[ChildProfile]) -> list[dict[str, Any]]:
    cards: list[dict[str, Any]] = []
    for child in children:
        cards.append(
            {
                "id": str(child.id),
                "name": _child_profile_display_name(child),
                "status": "Archived",
                "detail": _child_profile_birth_label(child),
                "notes": (child.notes or "").strip() or "No extra notes saved.",
                "restore_url": f"/app/child/{child.id}/restore",
                "remove_url": f"/app/child/{child.id}/remove",
                "next": _child_profile_return_url(request),
            }
        )
    return cards


def _parse_child_birth_year(value: str | None) -> int | None:
    raw = str(value or "").strip()
    if not raw:
        return None
    try:
        parsed = int(raw)
    except ValueError as exc:
        raise ValueError("birth_year") from exc
    if parsed < 1900 or parsed > datetime.now().year + 2:
        raise ValueError("birth_year")
    return parsed


def _split_csv(value: str | None) -> list[str]:
    if not value:
        return []
    return [item.strip() for item in str(value).split(",") if item.strip()]


async def _sync_child_seat_change_if_needed(
    session: AsyncSession,
    *,
    billing_service,
    account_id,
    subscription,
    current_active_count: int,
    next_active_count: int,
) -> None:
    if subscription is None or not billing_service.subscription_blocks_new_checkout(subscription):
        return
    current_additional = billing_service.additional_child_count(current_active_count)
    next_additional = billing_service.additional_child_count(next_active_count)
    if current_additional == next_additional:
        return
    if not billing_service.available or not billing_service.additional_child_billing_configured():
        raise RuntimeError("billing")
    await billing_service.sync_additional_child_quantity(
        session,
        account_id=account_id,
        subscription=subscription,
        child_profile_count=next_active_count,
    )


def _selected_child_spotlight(
    child: ChildProfile | None,
    *,
    child_count: int,
    add_on_label: str | None = None,
) -> dict[str, Any] | None:
    if child is None:
        return None
    return {
        "name": _child_profile_display_name(child),
        "summary": (
            f"{_child_profile_display_name(child)} is the profile currently in focus for this portal. "
            f"{_household_profile_note(child_count, add_on_label=add_on_label)}"
        ),
        "status_label": _child_profile_companion_status(child),
        "notes": (child.notes or "").strip() or "No extra notes have been added for this profile yet.",
        "metrics": [
            {
                "label": "Birth year",
                "value": str(child.birth_year) if child.birth_year else "Not added yet",
                "detail": "A simple reference point for the family profile.",
            },
            {
                "label": "Portal focus",
                "value": "Currently selected",
                "detail": "Timeline, memories, safety, and parent chat all follow this profile.",
            },
            {
                "label": "Companion connection",
                "value": _child_profile_companion_status(child),
                "detail": "Whether this profile is already linked to an active companion user.",
            },
        ],
    }


def _child_roster_cards(
    request: Request,
    *,
    children: list[ChildProfile],
    selected_child_id: str,
) -> list[dict[str, str]]:
    cards: list[dict[str, str]] = []
    current_url = request.url.path
    if request.url.query:
        current_url = f"{current_url}?{request.url.query}"
    for child in children:
        cards.append(
            {
                "id": str(child.id),
                "name": _child_profile_display_name(child),
                "status": _child_profile_companion_status(child),
                "detail": _child_profile_birth_label(child),
                "href": _append_query_params(current_url, child_id=str(child.id)),
                "active": "true" if str(child.id) == selected_child_id else "false",
            }
        )
    return cards


def _billing_plan_label(plan_key: str | None) -> str:
    normalized = str(plan_key or "").strip().lower()
    if normalized == "voice":
        return "Resona Voice"
    if normalized == "chat":
        return "Resona Chat"
    return "Resona plan"


def _billing_plan_summary(plan_key: str | None) -> str:
    normalized = str(plan_key or "").strip().lower()
    if normalized == "voice":
        return "Voice keeps the full text experience in place while adding live calls, richer continuity, and a more expressive companion presence."
    if normalized == "chat":
        return "Chat keeps the experience text-first while preserving memory, guidance, and parent visibility across the household."
    return "Choose the plan that best fits how this household wants to stay connected."


def _billing_additional_child_label(billing_service, *, additional_child_count: int) -> str:
    monthly_usd = billing_service.additional_child_monthly_usd()
    if monthly_usd > 0:
        monthly_label = f"{_format_dashboard_currency(monthly_usd)} / month"
    else:
        monthly_label = "monthly add-on"
    if additional_child_count <= 1:
        return monthly_label
    return f"{monthly_label} each"


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


def _memory_recent_page(value: str | None) -> int:
    try:
        return max(int(str(value or "").strip() or "1"), 1)
    except ValueError:
        return 1


def _memory_entity_kind_options() -> list[dict[str, str]]:
    return [
        {"value": "", "label": "Keep current placement"},
        *[
            {"value": kind.value, "label": kind.value.replace("_", " ").title()}
            for kind in MemoryEntityKind
        ],
    ]


def _memory_facet_options() -> list[dict[str, str]]:
    return [
        {"value": "", "label": "Keep current facet"},
        *[
            {"value": facet.value, "label": facet.value.replace("_", " ").title()}
            for facet in MemoryFacet
        ],
    ]


def _memory_truthy(value: str | None, *, default: bool = False) -> bool:
    normalized = str(value or "").strip().lower()
    if not normalized:
        return default
    return normalized in {"1", "true", "yes", "on"}


def _memory_toggle_query(value: str | None, *, default: bool) -> bool:
    normalized = str(value or "").strip().lower()
    if not normalized:
        return default
    if normalized in {"1", "true", "yes", "on"}:
        return True
    if normalized in {"0", "false", "no", "off"}:
        return False
    return default


def _memory_graph_query_state(request: Request) -> dict[str, Any]:
    query_params = request.query_params
    return {
        "include_archived": _memory_truthy(query_params.get("archived")),
        "search": str(query_params.get("q") or "").strip(),
        "type_filter": str(query_params.get("type") or "").strip(),
        "selected_node_id": str(query_params.get("node") or "").strip(),
        "branch_node_id": str(query_params.get("branch") or "").strip(),
        "show_similarity": _memory_toggle_query(query_params.get("similar"), default=True),
    }


def _memory_branch_context_from_graph(graph, *, branch_id: str) -> dict[str, Any] | None:
    if not branch_id:
        return None
    nodes = {node.id: node for node in list(graph.nodes or [])}
    branch_node = nodes.get(branch_id)
    if branch_node is None:
        return None
    if branch_node.kind == "memory":
        return {
            "node_id": branch_node.id,
            "label": branch_node.label,
            "memory_ids": [branch_node.id],
            "breadcrumb": [item.model_dump(mode="json") for item in branch_node.breadcrumb],
        }
    edges_by_source: dict[str, list[str]] = {}
    for edge in list(graph.structural_edges or []):
        edges_by_source.setdefault(edge.source, []).append(edge.target)
    discovered = {branch_id}
    queue = [branch_id]
    memory_ids: list[str] = []
    while queue:
        current = queue.pop(0)
        for neighbor_id in edges_by_source.get(current, []):
            if neighbor_id in discovered:
                continue
            discovered.add(neighbor_id)
            neighbor = nodes.get(neighbor_id)
            if neighbor is None:
                continue
            if neighbor.kind == "memory":
                memory_ids.append(neighbor.id)
                continue
            queue.append(neighbor_id)
    return {
        "node_id": branch_node.id,
        "label": branch_node.label,
        "memory_ids": memory_ids,
        "breadcrumb": [item.model_dump(mode="json") for item in branch_node.breadcrumb],
    }


def _portal_chat_context_items(customer_user: CustomerUser, child: ChildProfile | None) -> list[dict[str, str]]:
    if child is None:
        return []
    preferences = dict(child.preferences_json or {})
    boundaries = dict(child.boundaries_json or {})
    routines = dict(child.routines_json or {})
    return [
        {"label": "You are chatting as", "value": f"{_humanize_profile_value(customer_user.relationship_label)} of {child.display_name or child.first_name}"},
        {"label": "Preferred pacing", "value": _humanize_profile_value(preferences.get("preferred_pacing"))},
        {"label": "Response style", "value": _humanize_profile_value(preferences.get("response_style"))},
        {"label": "Voice enabled", "value": _humanize_profile_value(preferences.get("voice_enabled"))},
        {"label": "Parent visibility", "value": _humanize_profile_value(boundaries.get("parent_visibility_mode"))},
        {"label": "Alert threshold", "value": _humanize_profile_value(boundaries.get("alert_threshold"))},
        {"label": "Daily cadence", "value": _humanize_profile_value(routines.get("daily_cadence"))},
    ]


def _portal_chat_starters(child: ChildProfile | None) -> list[str]:
    child_name = child.display_name if child and child.display_name else (child.first_name if child else "my child")
    return [
        f"What should Resona lean into more with {child_name}?",
        f"What should Resona avoid doing with {child_name}?",
        f"Help me phrase a few do and don't rules for {child_name}.",
        f"What questions do you have for me so Resona understands {child_name} better?",
    ]


async def _portal_parent_chat_page_state(
    request: Request,
    session: AsyncSession,
    context: PortalRequestContext,
    *,
    child: ChildProfile | None,
    thread_id: str | None = None,
) -> tuple[PortalChatThread | None, list[dict[str, Any]], list[dict[str, Any]], dict[str, Any]]:
    chat_messages: list[dict[str, Any]] = []
    chat_threads: list[dict[str, Any]] = []
    thread = None
    persona = await _portal_child_persona(session, child=child)
    assistant_label = persona.display_name if persona and persona.display_name else "Resona"
    parent_chat_service = getattr(context.container, "parent_chat_service", None)
    if child and child.companion_user_id and parent_chat_service is not None:
        thread = await parent_chat_service.resolve_thread(
            session,
            account_id=context.customer_user.account_id,
            customer_user=context.customer_user,
            child_profile=child,
            thread_id=thread_id,
            create_if_missing=False,
        )
        chat_threads = [
            item.model_dump(mode="json")
            for item in parent_chat_service.serialize_threads(
                await parent_chat_service.list_threads(
                    session,
                    account_id=context.customer_user.account_id,
                    customer_user=context.customer_user,
                    child_profile=child,
                ),
                active_thread_id=thread.id if thread else None,
            )
        ]
        if thread is not None:
            chat_messages = [
                message.model_dump(mode="json")
                for message in parent_chat_service.serialize_messages(
                    await parent_chat_service.list_messages(session, thread_id=thread.id)
                )
            ]

    payload = {
        "csrf_token": context.csrf_token,
        "send_url": "/app/parent-chat/send",
        "stream_url": "/app/parent-chat/stream",
        "resume_url": request.url.path,
        "thread_id": str(thread.id) if thread else "",
        "new_chat_url": "/app/parent-chat/new",
        "clear_chat_url": f"/app/parent-chat/{thread.id}/clear" if thread else "",
        "child_name": child.display_name if child and child.display_name else (child.first_name if child else "your child"),
        "assistant_label": assistant_label,
        "memory_map_url": "/app/memories/map",
        "memory_library_url": "/app/memories/library",
        "status_message": "Parent chat is unavailable right now." if request.query_params.get("error") == "unavailable" else "",
        "status_tone": "danger" if request.query_params.get("error") == "unavailable" else "muted",
    }
    if request.query_params.get("cleared") == "1":
        payload["status_message"] = "Chat history cleared."
        payload["status_tone"] = "success"

    return thread, chat_messages, chat_threads, payload


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
        _clear_selected_child_cookie(response)
        return response

    token = request.cookies.get(container.settings.customer_portal.session_cookie_name)
    if token:
        await container.customer_auth_service.revoke_portal_session(session, raw_token=token)
        await session.commit()

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
    response.delete_cookie(container.settings.customer_portal.session_cookie_name)
    _clear_security_confirm_cookie(response)
    _clear_selected_child_cookie(response)
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
        portal_session_token, _ = await container.customer_auth_service.create_portal_session(
            session,
            customer_user=tenant.customer_user,
            user_agent=request.headers.get("user-agent"),
            ip_address=_client_ip(request),
            trusted_device=True,
        )
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
        container.clerk_auth_service.create_portal_session_token(tenant),
        httponly=True,
        secure=container.settings.customer_portal.secure_cookies or request.url.scheme == "https",
        samesite="lax",
        max_age=container.settings.customer_portal.session_max_age_seconds,
    )
    response.set_cookie(
        container.settings.customer_portal.session_cookie_name,
        portal_session_token,
        httponly=True,
        secure=container.settings.customer_portal.secure_cookies or request.url.scheme == "https",
        samesite="lax",
        max_age=container.settings.customer_portal.session_max_age_seconds,
    )
    return response


@router.post("/auth/clear")
async def portal_clerk_auth_clear(
    request: Request,
    session: AsyncSession = Depends(get_db_session),
):
    container = request.app.state.container
    portal_token = request.cookies.get(container.settings.customer_portal.session_cookie_name)
    if portal_token:
        try:
            await container.customer_auth_service.revoke_portal_session(session, raw_token=portal_token)
            await session.commit()
        except Exception:
            await session.rollback()
    response = JSONResponse({"ok": True})
    response.delete_cookie(container.settings.clerk.backend_session_cookie_name)
    response.delete_cookie(container.settings.customer_portal.session_cookie_name)
    _clear_security_confirm_cookie(response)
    _clear_selected_child_cookie(response)
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
        "resona_presets": context.container.portal_initialization_service.resona_preset_options(),
        "voice_profiles": context.container.portal_initialization_service.voice_profile_options(),
        "save_url": "/app/initialize/save",
        "preview_url": "/app/initialize/preview",
        "resona_preview_url": "/app/initialize/resona-preview",
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
        resona_presets=context.container.portal_initialization_service.resona_preset_options(),
        voice_profiles=context.container.portal_initialization_service.voice_profile_options(),
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


@router.post("/initialize/resona-preview")
async def portal_initialize_resona_preview(
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

    child_name = str(payload.get("child_name") or payload.get("profile_name") or "").strip()
    mode = "custom" if str(payload.get("resona_mode") or "").strip() == "custom" else "preset"
    preset_key = str(payload.get("resona_preset_key") or "").strip() or None
    voice_profile_key = str(payload.get("resona_voice_profile_key") or "").strip() or None

    temp_persona = Persona(key=f"preview-{uuid.uuid4().hex[:16]}", display_name="Resona")
    apply_portal_resona_to_persona(
        context.container.settings,
        persona=temp_persona,
        account_id=context.customer_user.account_id,
        owner_user_id=None,
        child_name=child_name,
        mode=mode,
        preset_key=preset_key,
        display_name=str(payload.get("resona_display_name") or "").strip() or None,
        voice_profile_key=voice_profile_key,
        vibe=str(payload.get("resona_vibe") or "").strip() or None,
        support_style=str(payload.get("resona_support_style") or "").strip() or None,
        avoid_text=str(payload.get("resona_avoid") or "").strip() or None,
        anchors_text=str(payload.get("resona_anchors") or "").strip() or None,
        proactive_style=str(payload.get("resona_proactive_style") or "").strip() or None,
    )
    preview_text = preview_text_for_name(
        temp_persona.display_name,
        fallback_name="Resona" if mode == "custom" else (temp_persona.display_name or "Resona"),
    )

    try:
        asset = await context.container.voice_service.generate_creative_audio_clip(
            session,
            persona=temp_persona,
            user=None,
            text=preview_text,
        )
        await session.commit()
    except RuntimeError as exc:
        await session.rollback()
        return JSONResponse({"ok": False, "detail": str(exc)}, status_code=503)

    if asset.generation_status != "ready":
        await session.rollback()
        return JSONResponse(
            {"ok": False, "detail": asset.error_message or "Voice preview is unavailable right now."},
            status_code=503,
        )

    return JSONResponse(
        {
            "ok": True,
            "asset_id": str(asset.id),
            "audio_url": f"/api/media/{asset.id}",
            "preview_text": preview_text,
            "voice_label": find_voice_profile(context.container.settings, voice_profile_key).label
            if find_voice_profile(context.container.settings, voice_profile_key)
            else "Voice preview",
        }
    )


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
    child_profiles, _ = await _portal_child_scope(request, session, account_id=context.customer_user.account_id)
    try:
        url = await context.container.billing_service.create_checkout_session(
            session,
            account=account,
            customer_email=context.customer_user.email,
            clerk_org_id=context.clerk_org_id,
            plan_key=plan_key,
            child_profile_count=len(child_profiles),
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
    children, child = await _portal_child_scope(request, session, account_id=context.customer_user.account_id)
    child_count = len(children)
    subscription = await context.container.billing_service.get_account_subscription(
        session,
        account_id=context.customer_user.account_id,
    )
    usage_summary = await context.container.billing_service.usage_credit_summary(
        session,
        account_id=context.customer_user.account_id,
        subscription=subscription,
    )
    subscription_status = subscription.status.value if subscription else SubscriptionStatus.incomplete.value
    subscription_label = _humanize_choice(subscription_status)
    role_label = _humanize_choice(context.role.value)

    messages, memory_items, safety_events = await _portal_child_activity_snapshot(
        session,
        child=child,
        message_limit=4,
        memory_limit=4,
        safety_limit=4,
    )
    plan_cards = _guidance_plan_cards(
        child=child,
        memory_items=memory_items,
        safety_events=safety_events,
    )
    question_cards = _guidance_question_cards(
        child=child,
        memory_items=memory_items,
        safety_events=safety_events,
    )

    parent_context = ParentDashboardContext(
        household_name=household.name if household else "Household",
        child_name=_child_profile_display_name(child),
        subscription_status=subscription_status,
        subscription_label=subscription_label,
        role_label=role_label,
        household_summary=_dashboard_household_summary(
            household=household,
            child=child,
            child_count=child_count,
            subscription_label=subscription_label,
        ),
        child_status_label=(
            f"{child_count} profile{'s' if child_count != 1 else ''} active"
            if child_count
            else "No child linked"
        ),
        child_status_detail=(
            f"Currently focused on {_child_profile_display_name(child)}."
            if child_count and child
            else "Finish child setup to unlock live conversation, memory, and safety context."
        ),
        usage_credit_summary=usage_summary,
        usage_hero=_dashboard_usage_hero(
            usage_summary,
            subscription_status=subscription_status,
        ),
        status_items=_dashboard_status_items(
            subscription_status=subscription_status,
            child=child,
            child_count=child_count,
            role_label=role_label,
            safety_events=safety_events,
        ),
        insights=_dashboard_insight_cards(
            child=child,
            memory_items=memory_items,
            plan_cards=plan_cards,
            question_cards=question_cards,
        ),
        conversation_previews=[_dashboard_message_preview(message) for message in messages],
        memory_previews=[_dashboard_memory_preview(item) for item in memory_items],
        safety_previews=[_dashboard_safety_preview(event) for event in safety_events],
        callout=_dashboard_callout(subscription_status),
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
    )


@router.get("/plans")
async def portal_plans_page(
    request: Request,
    session: AsyncSession = Depends(get_db_session),
    context: PortalRequestContext = Depends(require_portal_context),
):
    account = await session.get(Account, context.customer_user.account_id)
    household = await session.scalar(select(Household).where(Household.account_id == context.customer_user.account_id))
    _, child = await _portal_child_scope(request, session, account_id=context.customer_user.account_id)
    _, memory_items, safety_events = await _portal_child_activity_snapshot(
        session,
        child=child,
        message_limit=0,
        memory_limit=6,
        safety_limit=4,
    )
    memory_title, memory_summary = _recent_memory_signal(memory_items)
    child_name = _child_profile_display_name(child)
    page_summary = (
        f"A practical place to shape how everyday support should feel for {child_name}, using the profile, routine, and memory context already on this account."
        if child
        else "A practical place to turn profile context into clear support plans once a child profile is linked."
    )

    return _portal_response(
        request,
        "portal/plans.html",
        customer_user=context.customer_user,
        account=account,
        household=household,
        child=child,
        mfa_verified=context.mfa_verified,
        role=context.role.value,
        plan_cards=_guidance_plan_cards(
            child=child,
            memory_items=memory_items,
            safety_events=safety_events,
        ),
        page_summary=page_summary,
        focus_memory_title=memory_title,
        focus_memory_summary=memory_summary,
        selected_child_name=child_name,
    )


@router.get("/questions")
async def portal_questions_page(
    request: Request,
    session: AsyncSession = Depends(get_db_session),
    context: PortalRequestContext = Depends(require_portal_context),
):
    account = await session.get(Account, context.customer_user.account_id)
    household = await session.scalar(select(Household).where(Household.account_id == context.customer_user.account_id))
    _, child = await _portal_child_scope(request, session, account_id=context.customer_user.account_id)
    persona = await _portal_child_persona(session, child=child)
    _, memory_items, safety_events = await _portal_child_activity_snapshot(
        session,
        child=child,
        message_limit=0,
        memory_limit=6,
        safety_limit=4,
    )
    child_name = _child_profile_display_name(child)
    assistant_label = persona.display_name if persona and persona.display_name else "Resona"
    page_summary = (
        f"The highest-value questions to answer next for {child_name}, so {assistant_label} can become more grounded, accurate, and useful over time."
        if child
        else "The highest-value parent questions will appear here once a child profile is linked and there is some household context to work with."
    )
    thread, chat_messages, _, chat_payload = await _portal_parent_chat_page_state(
        request,
        session,
        context,
        child=child,
        thread_id=request.query_params.get("thread"),
    )

    return _portal_response(
        request,
        "portal/questions.html",
        customer_user=context.customer_user,
        account=account,
        household=household,
        child=child,
        mfa_verified=context.mfa_verified,
        role=context.role.value,
        question_cards=_guidance_question_cards(
            child=child,
            memory_items=memory_items,
            safety_events=safety_events,
        ),
        page_summary=page_summary,
        selected_child_name=child_name,
        selected_question_key=str(request.query_params.get("question") or "").strip(),
        assistant_label=assistant_label,
        active_thread_id=str(thread.id) if thread else "",
        chat_messages=chat_messages,
        chat_payload=chat_payload,
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
    children, selected_child = await _portal_child_scope(request, session, account_id=context.customer_user.account_id)
    child_count = len(children)
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
    plan_key = (
        context.container.billing_service.plan_key_for_subscription(subscription)
        or init_result.context.selected_plan_key
        or "chat"
    )
    additional_child_count = context.container.billing_service.additional_child_count(child_count)
    additional_child_label = _billing_additional_child_label(
        context.container.billing_service,
        additional_child_count=additional_child_count,
    )
    subscription_label = _humanize_choice(subscription.status.value if subscription else "incomplete")
    query_status = str(request.query_params.get("checkout") or "").strip().lower()
    query_error = str(request.query_params.get("error") or "").strip().lower()
    already_active = bool(request.query_params.get("already_active"))
    billing_notice = None
    if query_status == "success":
        billing_notice = {
            "tone": "success",
            "title": "Billing is active",
            "message": "Your subscription is confirmed and the household is ready to keep going.",
        }
    elif query_status == "cancel":
        billing_notice = {
            "tone": "warning",
            "title": "Checkout was canceled",
            "message": "No changes were made. You can return here any time when you're ready.",
        }
    elif already_active:
        billing_notice = {
            "tone": "warning",
            "title": "Billing is already active",
            "message": "No duplicate checkout was started for this account.",
        }
    elif query_error == "portal":
        billing_notice = {
            "tone": "danger",
            "title": "Manage billing is unavailable right now",
            "message": "We couldn't open the secure billing portal just now. Please try again in a moment.",
        }
    elif query_error == "stripe":
        billing_notice = {
            "tone": "danger",
            "title": "Billing couldn't start",
            "message": "We couldn't open checkout just now. Please try again in a moment.",
        }

    seat_status = "Included in your current plan"
    seat_note = "The first child profile is covered inside the main plan."
    if additional_child_count > 0:
        seat_status = f"{additional_child_count} additional profile{'s' if additional_child_count != 1 else ''}"
        seat_note = f"Additional child profiles are billed separately at {additional_child_label}."

    billing_context = {
        "plan_label": _billing_plan_label(plan_key),
        "plan_summary": _billing_plan_summary(plan_key),
        "subscription_label": subscription_label,
        "usage_hero": _dashboard_usage_hero(
            usage_summary,
            subscription_status=subscription.status.value if subscription else SubscriptionStatus.incomplete.value,
        ),
        "status_items": [
            {
                "label": "Plan status",
                "value": subscription_label,
                "detail": "Current subscription standing for this household.",
                "tone": _dashboard_tone_for_subscription(subscription.status.value if subscription else "incomplete"),
            },
            {
                "label": "Profiles on this account",
                "value": f"{child_count}",
                "detail": (
                    f"Currently focused on {_child_profile_display_name(selected_child)}."
                    if selected_child is not None
                    else "Add the first child profile to start personalizing the portal."
                ),
                "tone": "positive" if child_count else "warning",
            },
            {
                "label": "Included profiles",
                "value": str(context.container.billing_service.included_child_profiles()),
                "detail": "Profiles included before add-on billing begins.",
                "tone": "neutral",
            },
            {
                "label": "Additional child add-on",
                "value": seat_status,
                "detail": seat_note,
                "tone": "warning" if additional_child_count else "positive",
            },
        ],
        "plan_panel": {
            "eyebrow": "Plan overview",
            "title": _billing_plan_label(plan_key),
            "summary": _billing_plan_summary(plan_key),
            "items": [
                {"label": "Monthly included credits", "value": _format_dashboard_currency(float(usage_summary.included_usd or 0.0))},
                {"label": "Household profiles", "value": f"{child_count} active"},
                {
                    "label": "Current focus",
                    "value": _child_profile_display_name(selected_child) if selected_child else "No child linked yet",
                },
            ],
        },
        "seat_panel": {
            "eyebrow": "Household profiles",
            "title": "Plan one household with room to grow",
            "summary": _household_profile_note(child_count, add_on_label=additional_child_label),
            "items": [
                {"label": "Included in base plan", "value": f"{context.container.billing_service.included_child_profiles()} child profile"},
                {"label": "Additional profiles", "value": f"{additional_child_label}"},
                {
                    "label": "Current add-on count",
                    "value": f"{additional_child_count} active" if additional_child_count else "None right now",
                },
            ],
            "action_label": "Manage Profiles",
            "action_href": "/app/child",
        },
        "action_panel": {
            "eyebrow": "Billing actions",
            "title": "Keep the household aligned",
            "summary": (
                "Billing is already active for this account. Use the child profile area to add or review household profiles."
                if context.container.billing_service.subscription_blocks_new_checkout(subscription)
                else "When you're ready, start secure checkout to activate the household plan."
            ),
        },
        "manage_portal_available": bool(
            stripe_enabled := context.container.billing_service.available
        )
        and subscription is not None
        and context.container.billing_service.subscription_blocks_new_checkout(subscription)
        and bool(subscription.stripe_customer_id),
    }
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
        billing_context=billing_context,
        billing_notice=billing_notice,
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
    child_profiles, _ = await _portal_child_scope(request, session, account_id=context.customer_user.account_id)
    try:
        url = await context.container.billing_service.create_checkout_session(
            session,
            account=account,
            customer_email=context.customer_user.email,
            clerk_org_id=context.clerk_org_id,
            plan_key=plan_key,
            child_profile_count=len(child_profiles),
            success_url=success_url,
            cancel_url=cancel_url,
        )
    except RuntimeError:
        await session.commit()
        return RedirectResponse(url="/app/billing?error=stripe", status_code=303)
    await session.commit()
    return RedirectResponse(url=url, status_code=303)


@router.post("/billing/manage")
async def portal_billing_manage(
    request: Request,
    session: AsyncSession = Depends(get_db_session),
    context: PortalRequestContext = Depends(require_owner_mfa_context),
):
    await _verify_portal_csrf(request, context)
    subscription = await context.container.billing_service.get_account_subscription(
        session,
        account_id=context.customer_user.account_id,
    )
    if subscription is None or not context.container.billing_service.subscription_blocks_new_checkout(subscription):
        await session.rollback()
        return RedirectResponse(url="/app/billing?error=portal", status_code=303)
    try:
        url = await context.container.billing_service.create_customer_portal_session(
            session,
            account_id=context.customer_user.account_id,
            subscription=subscription,
            return_url=f"{context.container.settings.app.base_url}/app/billing",
        )
    except RuntimeError:
        await session.rollback()
        return RedirectResponse(url="/app/billing?error=portal", status_code=303)
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
    children, child = await _portal_child_scope(request, session, account_id=context.customer_user.account_id)
    active_children = _active_child_profiles(children)
    archived_children = [item for item in children if not item.is_active]
    child_count = len(active_children)
    subscription = await context.container.billing_service.get_account_subscription(
        session,
        account_id=context.customer_user.account_id,
    )
    additional_child_count = context.container.billing_service.additional_child_count(child_count)
    additional_child_label = _billing_additional_child_label(
        context.container.billing_service,
        additional_child_count=max(additional_child_count, 1),
    )
    child_profile_sections = _child_profile_sections(child) if child else []
    child_resona_summary = await _portal_child_resona_summary(
        session,
        settings=context.container.settings,
        child=child,
    )
    child_overview = _selected_child_spotlight(child, child_count=child_count, add_on_label=additional_child_label)
    child_notice = _child_notice_payload(request)
    can_remove_selected = await _child_profile_can_remove(session, child=child) if child else False
    add_child_blocked = bool(
        subscription
        and context.container.billing_service.subscription_blocks_new_checkout(subscription)
        and not context.container.billing_service.additional_child_billing_configured()
    )
    return _portal_response(
        request,
        "portal/child.html",
        customer_user=context.customer_user,
        child=child,
        child_overview=child_overview,
        child_resona_summary=child_resona_summary.model_dump(mode="json") if child_resona_summary else None,
        child_profile_sections=child_profile_sections,
        child_roster=_child_roster_cards(
            request,
            children=active_children,
            selected_child_id=str(child.id) if child else "",
        ),
        archived_child_roster=_archived_child_cards(request, children=archived_children),
        child_household_context={
            "count_label": f"{child_count} active profile{'s' if child_count != 1 else ''}",
            "summary": _household_profile_note(child_count, add_on_label=additional_child_label),
            "extra_note": (
                f"{len(archived_children)} archived profile{'s' if len(archived_children) != 1 else ''} kept off the active household roster."
                if archived_children
                else (
                    f"Any active profile beyond the first adds {additional_child_label} to the household plan."
                    if child_count > 1
                    else f"Additional active children can be added later for {additional_child_label}, with one active Resona included for each."
                )
            ),
        },
        child_notice=child_notice,
        child_edit_form=_child_form_payload(
            child,
            request=request,
            csrf_token=context.csrf_token,
            can_remove=can_remove_selected,
        ),
        child_resona_form=await _child_resona_form_payload(
            session,
            settings=context.container.settings,
            child=child,
            csrf_token=context.csrf_token,
        ),
        add_child_form={
            "action_url": "/app/child/add",
            "csrf_token": context.csrf_token,
            "disabled": add_child_blocked,
            "billing_note": (
                "Additional child billing is temporarily unavailable, so new profiles are paused until that finishes syncing."
                if add_child_blocked
                else f"The first child profile is included with one active Resona. Each additional profile is billed separately at {additional_child_label}."
            ),
        },
    )


@router.post("/child/add")
async def portal_child_add(
    request: Request,
    session: AsyncSession = Depends(get_db_session),
    context: PortalRequestContext = Depends(require_owner_mfa_context),
):
    await _verify_portal_csrf(request, context)
    form = await request.form()
    next_url = _safe_resume_path(str(form.get("next") or "/app/child"), default="/app/child")
    household = await session.scalar(select(Household).where(Household.account_id == context.customer_user.account_id))
    if household is None:
        return RedirectResponse(url=_append_query_params(next_url, error="setup"), status_code=303)

    first_name = " ".join(str(form.get("first_name") or "").strip().split())
    display_name = " ".join(str(form.get("display_name") or "").strip().split()) or first_name
    notes = str(form.get("notes") or "").strip() or None
    birth_year_raw = str(form.get("birth_year") or "").strip()
    if not first_name:
        return RedirectResponse(url=_append_query_params(next_url, error="name"), status_code=303)

    birth_year = None
    if birth_year_raw:
        try:
            birth_year = int(birth_year_raw)
        except ValueError:
            return RedirectResponse(url=_append_query_params(next_url, error="birth_year"), status_code=303)
        if birth_year < 1900 or birth_year > datetime.now().year + 2:
            return RedirectResponse(url=_append_query_params(next_url, error="birth_year"), status_code=303)

    children, _ = await _portal_child_scope(request, session, account_id=context.customer_user.account_id)
    prospective_child_count = len(children) + 1
    billing_service = context.container.billing_service
    subscription = await billing_service.get_account_subscription(session, account_id=context.customer_user.account_id)
    needs_billing_sync = (
        subscription is not None
        and billing_service.subscription_blocks_new_checkout(subscription)
        and billing_service.additional_child_count(prospective_child_count) > 0
    )
    if needs_billing_sync and (not billing_service.available or not billing_service.additional_child_billing_configured()):
        return RedirectResponse(url=_append_query_params(next_url, error="billing"), status_code=303)

    child = ChildProfile(
        account_id=context.customer_user.account_id,
        household_id=household.id,
        first_name=first_name,
        display_name=display_name,
        birth_year=birth_year,
        notes=notes,
        preferences_json={},
        boundaries_json={},
        routines_json={},
        is_active=True,
    )
    session.add(child)
    await session.flush()

    if needs_billing_sync and subscription is not None:
        try:
            await billing_service.sync_additional_child_quantity(
                session,
                account_id=context.customer_user.account_id,
                subscription=subscription,
                child_profile_count=prospective_child_count,
            )
        except RuntimeError:
            await session.rollback()
            return RedirectResponse(url=_append_query_params(next_url, error="billing"), status_code=303)

    await session.commit()
    return RedirectResponse(
        url=_append_query_params("/app/child", child_id=str(child.id), added="1"),
        status_code=303,
    )


@router.post("/child/{child_id}/update")
async def portal_child_update(
    child_id: str,
    request: Request,
    session: AsyncSession = Depends(get_db_session),
    context: PortalRequestContext = Depends(require_owner_mfa_context),
):
    await _verify_portal_csrf(request, context)
    form = await request.form()
    next_url = _safe_resume_path(str(form.get("next") or "/app/child"), default="/app/child")
    child = await _owned_child_profile(session, account_id=context.customer_user.account_id, child_id=child_id)
    if child is None:
        return RedirectResponse(url=_append_query_params(next_url, error="not_found"), status_code=303)

    first_name = " ".join(str(form.get("first_name") or "").strip().split())
    display_name = " ".join(str(form.get("display_name") or "").strip().split()) or first_name
    notes = str(form.get("notes") or "").strip() or None
    if not first_name:
        return RedirectResponse(url=_append_query_params(next_url, error="name"), status_code=303)
    try:
        birth_year = _parse_child_birth_year(form.get("birth_year"))
    except ValueError:
        return RedirectResponse(url=_append_query_params(next_url, error="birth_year"), status_code=303)

    child.first_name = first_name
    child.display_name = display_name
    child.birth_year = birth_year
    child.notes = notes
    await session.commit()
    return RedirectResponse(
        url=_append_query_params("/app/child", child_id=str(child.id), updated="1", error=None),
        status_code=303,
    )


@router.post("/child/{child_id}/resona")
async def portal_child_resona_update(
    child_id: str,
    request: Request,
    session: AsyncSession = Depends(get_db_session),
    context: PortalRequestContext = Depends(require_owner_mfa_context),
):
    await _verify_portal_csrf(request, context)
    form = await request.form()
    child = await _owned_child_profile(session, account_id=context.customer_user.account_id, child_id=child_id)
    if child is None:
        return RedirectResponse(url="/app/child?error=not_found", status_code=303)

    companion_user = await session.get(User, child.companion_user_id) if child.companion_user_id else None
    persona = await _portal_child_persona(session, child=child)
    if persona is None or persona.source_type == "admin" or (persona.account_id and persona.account_id != child.account_id):
        persona = Persona(key=f"portal-{uuid.uuid4().hex[:16]}", display_name="Resona")
        session.add(persona)
        await session.flush()

    mode = "custom" if str(form.get("resona_mode") or "").strip() == "custom" else "preset"
    preset_key = str(form.get("resona_preset_key") or "").strip() or None
    voice_profile_key = str(form.get("resona_voice_profile_key") or "").strip() or None
    apply_portal_resona_to_persona(
        context.container.settings,
        persona=persona,
        account_id=context.customer_user.account_id,
        owner_user_id=companion_user.id if companion_user else None,
        child_name=_child_profile_display_name(child),
        mode=mode,
        preset_key=preset_key,
        display_name=str(form.get("resona_display_name") or "").strip() or None,
        voice_profile_key=voice_profile_key,
        vibe=str(form.get("resona_vibe") or "").strip() or None,
        support_style=str(form.get("resona_support_style") or "").strip() or None,
        avoid_text=str(form.get("resona_avoid") or "").strip() or None,
        anchors_text=str(form.get("resona_anchors") or "").strip() or None,
        proactive_style=str(form.get("resona_proactive_style") or "").strip() or None,
    )

    preferences = dict(child.preferences_json or {})
    preferences["resona_profile"] = {
        "mode": mode,
        "preset_key": preset_key,
        "display_name": persona.display_name,
        "voice_profile_key": voice_profile_key,
        "vibe": str(form.get("resona_vibe") or "").strip(),
        "support_style": str(form.get("resona_support_style") or "").strip(),
        "avoid": str(form.get("resona_avoid") or "").strip(),
        "anchors": str(form.get("resona_anchors") or "").strip(),
        "proactive_style": str(form.get("resona_proactive_style") or "").strip(),
    }
    persona.description = str(form.get("resona_description") or "").strip() or None
    persona.style = str(form.get("resona_style") or "").strip() or None
    persona.tone = str(form.get("resona_tone") or "").strip() or None
    persona.boundaries = str(form.get("resona_boundaries") or "").strip() or None
    persona.topics_of_interest = _split_csv(str(form.get("resona_topics") or ""))
    persona.favorite_activities = _split_csv(str(form.get("resona_activities") or ""))
    persona.speech_style = str(form.get("resona_speech_style") or "").strip() or None
    persona.disclosure_policy = str(form.get("resona_disclosure_style") or "").strip() or None
    persona.texting_length_preference = str(form.get("resona_texting_length") or "").strip() or None
    persona.emoji_tendency = str(form.get("resona_emoji_tendency") or "").strip() or None
    persona.operator_notes = str(form.get("resona_parent_notes") or "").strip() or None

    if companion_user is not None:
        companion_user.preferred_persona_id = persona.id
        persona.owner_user_id = companion_user.id
        preferences.pop("pending_persona_id", None)
    else:
        preferences["pending_persona_id"] = str(persona.id)
    child.preferences_json = preferences

    await session.commit()
    return RedirectResponse(
        url=_append_query_params("/app/child", child_id=str(child.id), resona="1", error=None),
        status_code=303,
    )


@router.post("/child/{child_id}/resona/preview")
async def portal_child_resona_preview(
    child_id: str,
    request: Request,
    session: AsyncSession = Depends(get_db_session),
    context: PortalRequestContext = Depends(require_owner_mfa_context),
):
    try:
        payload = await request.json()
    except Exception as exc:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="Invalid request body") from exc
    csrf_token = str(payload.get("csrf_token") or "")
    if not csrf_token or csrf_token != context.csrf_token:
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="Invalid CSRF token")

    child = await _owned_child_profile(session, account_id=context.customer_user.account_id, child_id=child_id)
    if child is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Child profile not found")

    temp_persona = Persona(key=f"preview-{uuid.uuid4().hex[:16]}", display_name="Resona")
    mode = "custom" if str(payload.get("resona_mode") or "").strip() == "custom" else "preset"
    voice_profile_key = str(payload.get("resona_voice_profile_key") or "").strip() or None
    apply_portal_resona_to_persona(
        context.container.settings,
        persona=temp_persona,
        account_id=context.customer_user.account_id,
        owner_user_id=None,
        child_name=_child_profile_display_name(child),
        mode=mode,
        preset_key=str(payload.get("resona_preset_key") or "").strip() or None,
        display_name=str(payload.get("resona_display_name") or "").strip() or None,
        voice_profile_key=voice_profile_key,
        vibe=str(payload.get("resona_vibe") or "").strip() or None,
        support_style=str(payload.get("resona_support_style") or "").strip() or None,
        avoid_text=str(payload.get("resona_avoid") or "").strip() or None,
        anchors_text=str(payload.get("resona_anchors") or "").strip() or None,
        proactive_style=str(payload.get("resona_proactive_style") or "").strip() or None,
    )
    preview_text = preview_text_for_name(temp_persona.display_name, fallback_name="Resona")
    try:
        asset = await context.container.voice_service.generate_creative_audio_clip(
            session,
            persona=temp_persona,
            user=None,
            text=preview_text,
        )
        await session.commit()
    except RuntimeError as exc:
        await session.rollback()
        return JSONResponse({"ok": False, "detail": str(exc)}, status_code=503)
    if asset.generation_status != "ready":
        await session.rollback()
        return JSONResponse(
            {"ok": False, "detail": asset.error_message or "Voice preview is unavailable right now."},
            status_code=503,
        )
    voice = find_voice_profile(context.container.settings, voice_profile_key)
    return JSONResponse(
        {
            "ok": True,
            "asset_id": str(asset.id),
            "audio_url": f"/api/media/{asset.id}",
            "preview_text": preview_text,
            "voice_label": voice.label if voice else "Voice preview",
        }
    )


@router.post("/child/{child_id}/archive")
async def portal_child_archive(
    child_id: str,
    request: Request,
    session: AsyncSession = Depends(get_db_session),
    context: PortalRequestContext = Depends(require_owner_mfa_context),
):
    await _verify_portal_csrf(request, context)
    children, _ = await _portal_child_scope(request, session, account_id=context.customer_user.account_id)
    current_active_count = len(_active_child_profiles(children))
    child = await _owned_child_profile(session, account_id=context.customer_user.account_id, child_id=child_id)
    if child is None:
        return RedirectResponse(url="/app/child?error=not_found", status_code=303)
    if not child.is_active:
        return RedirectResponse(url="/app/child?archived=1", status_code=303)

    subscription = await context.container.billing_service.get_account_subscription(
        session,
        account_id=context.customer_user.account_id,
    )
    next_active_count = max(current_active_count - 1, 0)
    try:
        await _sync_child_seat_change_if_needed(
            session,
            billing_service=context.container.billing_service,
            account_id=context.customer_user.account_id,
            subscription=subscription,
            current_active_count=current_active_count,
            next_active_count=next_active_count,
        )
    except RuntimeError:
        await session.rollback()
        return RedirectResponse(url="/app/child?error=billing", status_code=303)

    child.is_active = False
    await session.commit()
    response = RedirectResponse(url="/app/child?archived=1", status_code=303)
    if next_active_count <= 0:
        _clear_selected_child_cookie(response)
    return response


@router.post("/child/{child_id}/restore")
async def portal_child_restore(
    child_id: str,
    request: Request,
    session: AsyncSession = Depends(get_db_session),
    context: PortalRequestContext = Depends(require_owner_mfa_context),
):
    await _verify_portal_csrf(request, context)
    children, _ = await _portal_child_scope(request, session, account_id=context.customer_user.account_id)
    current_active_count = len(_active_child_profiles(children))
    child = await _owned_child_profile(session, account_id=context.customer_user.account_id, child_id=child_id)
    if child is None:
        return RedirectResponse(url="/app/child?error=not_found", status_code=303)
    if child.is_active:
        return RedirectResponse(url=_append_query_params("/app/child", child_id=str(child.id), restored="1"), status_code=303)

    subscription = await context.container.billing_service.get_account_subscription(
        session,
        account_id=context.customer_user.account_id,
    )
    next_active_count = current_active_count + 1
    try:
        await _sync_child_seat_change_if_needed(
            session,
            billing_service=context.container.billing_service,
            account_id=context.customer_user.account_id,
            subscription=subscription,
            current_active_count=current_active_count,
            next_active_count=next_active_count,
        )
    except RuntimeError:
        await session.rollback()
        return RedirectResponse(url="/app/child?error=billing", status_code=303)

    child.is_active = True
    await session.commit()
    return RedirectResponse(
        url=_append_query_params("/app/child", child_id=str(child.id), restored="1", error=None),
        status_code=303,
    )


@router.post("/child/{child_id}/remove")
async def portal_child_remove(
    child_id: str,
    request: Request,
    session: AsyncSession = Depends(get_db_session),
    context: PortalRequestContext = Depends(require_owner_mfa_context),
):
    await _verify_portal_csrf(request, context)
    children, _ = await _portal_child_scope(request, session, account_id=context.customer_user.account_id)
    current_active_count = len(_active_child_profiles(children))
    child = await _owned_child_profile(session, account_id=context.customer_user.account_id, child_id=child_id)
    if child is None:
        return RedirectResponse(url="/app/child?error=not_found", status_code=303)
    if not await _child_profile_can_remove(session, child=child):
        return RedirectResponse(url=_append_query_params("/app/child", child_id=str(child.id), error="remove_blocked"), status_code=303)

    subscription = await context.container.billing_service.get_account_subscription(
        session,
        account_id=context.customer_user.account_id,
    )
    next_active_count = current_active_count - 1 if child.is_active else current_active_count
    try:
        await _sync_child_seat_change_if_needed(
            session,
            billing_service=context.container.billing_service,
            account_id=context.customer_user.account_id,
            subscription=subscription,
            current_active_count=current_active_count,
            next_active_count=max(next_active_count, 0),
        )
    except RuntimeError:
        await session.rollback()
        return RedirectResponse(url=_append_query_params("/app/child", child_id=str(child.id), error="billing"), status_code=303)

    await session.delete(child)
    await session.commit()
    response = RedirectResponse(url="/app/child?removed=1", status_code=303)
    if next_active_count <= 0:
        _clear_selected_child_cookie(response)
    return response


@router.get("/timeline")
async def portal_timeline(
    request: Request,
    session: AsyncSession = Depends(get_db_session),
    context: PortalRequestContext = Depends(require_portal_context),
):
    child = await _portal_child_profile(request, session, account_id=context.customer_user.account_id)
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


@router.get("/parent-chat")
async def portal_parent_chat(
    request: Request,
    session: AsyncSession = Depends(get_db_session),
    context: PortalRequestContext = Depends(require_portal_context),
):
    child = await _portal_child_profile(request, session, account_id=context.customer_user.account_id)
    thread, chat_messages, chat_threads, payload = await _portal_parent_chat_page_state(
        request,
        session,
        context,
        child=child,
        thread_id=request.query_params.get("thread"),
    )
    return _portal_response(
        request,
        "portal/parent_chat.html",
        customer_user=context.customer_user,
        child=child,
        chat_messages=chat_messages,
        chat_threads=chat_threads,
        active_thread_id=str(thread.id) if thread else "",
        chat_payload=payload,
        chat_context_items=_portal_chat_context_items(context.customer_user, child),
        chat_starter_prompts=_portal_chat_starters(child),
    )


@router.post("/parent-chat/new")
async def portal_parent_chat_new(
    request: Request,
    session: AsyncSession = Depends(get_db_session),
    context: PortalRequestContext = Depends(require_portal_context),
):
    content_type = (request.headers.get("content-type") or "").lower()
    expects_html_redirect = "application/x-www-form-urlencoded" in content_type or "multipart/form-data" in content_type
    if expects_html_redirect:
        payload = await request.form()
    else:
        payload = await request.json()
    csrf_token = str(payload.get("csrf_token") or "")
    if not csrf_token or csrf_token != context.csrf_token:
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="Invalid CSRF token")

    child = await _portal_child_profile(request, session, account_id=context.customer_user.account_id)
    if child is None or not child.companion_user_id:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="No child profile is linked yet.")

    thread = await context.container.parent_chat_service.create_thread(
        session,
        account_id=context.customer_user.account_id,
        customer_user=context.customer_user,
        child_profile=child,
    )
    await session.commit()
    redirect_url = f"/app/parent-chat?thread={thread.id}"
    if expects_html_redirect:
        return RedirectResponse(url=redirect_url, status_code=303)
    return JSONResponse({"ok": True, "thread_id": str(thread.id), "redirect_url": redirect_url})


@router.post("/parent-chat/send")
async def portal_parent_chat_send(
    request: Request,
    session: AsyncSession = Depends(get_db_session),
    context: PortalRequestContext = Depends(require_portal_context),
):
    content_type = (request.headers.get("content-type") or "").lower()
    expects_html_redirect = "application/x-www-form-urlencoded" in content_type or "multipart/form-data" in content_type
    if expects_html_redirect:
        payload = await request.form()
    else:
        payload = await request.json()
    csrf_token = str(payload.get("csrf_token") or "")
    if not csrf_token or csrf_token != context.csrf_token:
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="Invalid CSRF token")

    child = await _portal_child_profile(request, session, account_id=context.customer_user.account_id)
    if child is None or not child.companion_user_id:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="No child profile is linked yet.")
    thread = await context.container.parent_chat_service.resolve_thread(
        session,
        account_id=context.customer_user.account_id,
        customer_user=context.customer_user,
        child_profile=child,
        thread_id=str(payload.get("thread_id") or "").strip() or None,
    )
    if thread is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="That chat could not be found.")

    await _enforce_rate_limit(
        request,
        key=f"portal-parent-chat:{context.account_id}:{context.customer_user.id}",
        limit=40,
        window_seconds=3600,
    )
    try:
        _, parent_message, assistant_message = await context.container.parent_chat_service.send_message(
            session,
            account_id=context.customer_user.account_id,
            customer_user=context.customer_user,
            child_profile=child,
            text=str(payload.get("message") or ""),
            question_context=str(payload.get("question_context") or "").strip() or None,
            thread=thread,
        )
    except ValueError as exc:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=str(exc)) from exc
    except AIUnavailableError as exc:
        await session.rollback()
        if expects_html_redirect:
            return RedirectResponse(url="/app/parent-chat?error=unavailable", status_code=303)
        return JSONResponse(
            {
                "ok": False,
                "code": "ai_unavailable",
                "detail": str(exc),
                "resume_url": "/app/parent-chat",
                "retryable": True,
            },
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
        )
    await session.commit()

    if expects_html_redirect:
        return RedirectResponse(url=f"/app/parent-chat?thread={thread.id}&sent=1", status_code=303)

    return JSONResponse(
        {
            "ok": True,
            "thread_id": str(thread.id),
            "messages": [
                message.model_dump(mode="json")
                for message in context.container.parent_chat_service.serialize_messages([parent_message, assistant_message])
            ],
        }
    )


@router.post("/parent-chat/stream")
async def portal_parent_chat_stream(
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

    child = await _portal_child_profile(request, session, account_id=context.customer_user.account_id)
    if child is None or not child.companion_user_id:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="No child profile is linked yet.")
    thread = await context.container.parent_chat_service.resolve_thread(
        session,
        account_id=context.customer_user.account_id,
        customer_user=context.customer_user,
        child_profile=child,
        thread_id=str(payload.get("thread_id") or "").strip() or None,
    )
    if thread is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="That chat could not be found.")

    await _enforce_rate_limit(
        request,
        key=f"portal-parent-chat:{context.account_id}:{context.customer_user.id}",
        limit=40,
        window_seconds=3600,
    )

    async def _event_stream():
        try:
            async for event in context.container.parent_chat_service.stream_message(
                session,
                account_id=context.customer_user.account_id,
                customer_user=context.customer_user,
                child_profile=child,
                text=str(payload.get("message") or ""),
                question_context=str(payload.get("question_context") or "").strip() or None,
                thread=thread,
            ):
                yield f"event: {event.get('type', 'message')}\n".encode("utf-8")
                yield f"data: {json.dumps(event)}\n\n".encode("utf-8")
        except ValueError as exc:
            error_payload = {"type": "run_error", "detail": str(exc), "retryable": False}
            yield f"event: run_error\n".encode("utf-8")
            yield f"data: {json.dumps(error_payload)}\n\n".encode("utf-8")
        except AIUnavailableError as exc:
            error_payload = {"type": "run_error", "detail": str(exc), "retryable": True}
            yield f"event: run_error\n".encode("utf-8")
            yield f"data: {json.dumps(error_payload)}\n\n".encode("utf-8")

    response = StreamingResponse(_event_stream(), media_type="text/event-stream")
    response.headers["Cache-Control"] = "no-cache"
    response.headers["X-Accel-Buffering"] = "no"
    return response


@router.post("/parent-chat/{thread_id}/clear")
async def portal_parent_chat_clear(
    thread_id: str,
    request: Request,
    session: AsyncSession = Depends(get_db_session),
    context: PortalRequestContext = Depends(require_portal_context),
):
    content_type = (request.headers.get("content-type") or "").lower()
    expects_html_redirect = "application/x-www-form-urlencoded" in content_type or "multipart/form-data" in content_type
    if expects_html_redirect:
        payload = await request.form()
    else:
        payload = await request.json()
    csrf_token = str(payload.get("csrf_token") or "")
    if not csrf_token or csrf_token != context.csrf_token:
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="Invalid CSRF token")

    child = await _portal_child_profile(request, session, account_id=context.customer_user.account_id)
    if child is None or not child.companion_user_id:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="No child profile is linked yet.")
    thread = await context.container.parent_chat_service.resolve_thread(
        session,
        account_id=context.customer_user.account_id,
        customer_user=context.customer_user,
        child_profile=child,
        thread_id=thread_id,
        create_if_missing=False,
    )
    if thread is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="That chat could not be found.")

    await context.container.parent_chat_service.clear_thread(session, thread=thread)
    await session.commit()
    redirect_url = f"/app/parent-chat?thread={thread.id}&cleared=1"
    if expects_html_redirect:
        return RedirectResponse(url=redirect_url, status_code=303)
    return JSONResponse({"ok": True, "thread_id": str(thread.id), "messages": []})


async def _render_portal_memories_page(
    request: Request,
    session: AsyncSession,
    context: PortalRequestContext,
    *,
    view: str,
):
    child = await _portal_child_profile(request, session, account_id=context.customer_user.account_id)
    memory_user_id = child.companion_user_id if child else None
    query_params = request.query_params
    query_state = _memory_graph_query_state(request)
    include_archived = bool(query_state["include_archived"])
    search = str(query_state["search"] or "")
    type_filter = str(query_state["type_filter"] or "") if view == "library" else ""
    selected_node_id = str(query_state["selected_node_id"] or "")
    branch_node_id = str(query_state["branch_node_id"] or "")
    show_similarity = bool(query_state["show_similarity"])
    recent_page = _memory_recent_page(query_params.get("recent_page"))
    recent_page_size = 5

    memory_items: list[MemoryItem] = []
    recent_memory_items: list[MemoryItem] = []
    recent_memory_total = 0
    memory_branch_context = None
    if memory_user_id and branch_node_id:
        branch_graph = await context.container.memory_service.graph_snapshot(
            session,
            user_id=memory_user_id,
            include_archived=include_archived,
            limit=140,
        )
        memory_branch_context = _memory_branch_context_from_graph(branch_graph, branch_id=branch_node_id)
        if memory_branch_context is None:
            fallback_memory = await context.container.memory_service.memory_inspector(
                session,
                user_id=memory_user_id,
                memory_id=branch_node_id,
            )
            if fallback_memory is not None:
                memory_branch_context = {
                    "node_id": fallback_memory.id,
                    "label": fallback_memory.title,
                    "memory_ids": [fallback_memory.id],
                    "breadcrumb": [item.model_dump(mode="json") for item in fallback_memory.breadcrumb],
                }
    if memory_user_id:
        memory_items = await context.container.memory_service.list_memories_for_user(
            session,
            user_id=memory_user_id,
            include_archived=include_archived,
            search=(search or None) if view == "library" else None,
            memory_type=type_filter or None,
            limit=120,
        )
        if view == "library" and memory_branch_context:
            allowed_ids = set(memory_branch_context.get("memory_ids") or [])
            memory_items = [item for item in memory_items if str(item.id) in allowed_ids]
        if view in {"map", "routine"}:
            recent_world_section = "daily_routine" if view == "routine" else "memories"
            recent_memory_items, recent_memory_total = await context.container.memory_service.list_recent_memories_for_user(
                session,
                user_id=memory_user_id,
                include_archived=include_archived,
                world_section=recent_world_section,
                page=recent_page,
                page_size=recent_page_size,
            )
            recent_page_total = max((recent_memory_total + recent_page_size - 1) // recent_page_size, 1)
            if recent_memory_total and recent_page > recent_page_total:
                recent_page = recent_page_total
                recent_memory_items, recent_memory_total = await context.container.memory_service.list_recent_memories_for_user(
                    session,
                    user_id=memory_user_id,
                    include_archived=include_archived,
                    world_section=recent_world_section,
                    page=recent_page,
                    page_size=recent_page_size,
                )
        else:
            recent_page_total = 1
    else:
        recent_page_total = 1

    current_url = request.url.path
    if request.url.query:
        current_url = f"{current_url}?{request.url.query}"
    recent_prev_url = None
    recent_next_url = None
    if view in {"map", "routine"} and recent_memory_total:
        if recent_page > 1:
            recent_prev_url = _append_query_params(current_url, recent_page=None if recent_page - 1 <= 1 else str(recent_page - 1))
        if recent_page < recent_page_total:
            recent_next_url = _append_query_params(current_url, recent_page=str(recent_page + 1))
    current_url = _append_query_params(current_url, memory_store_status=None, memory_store_error=None)
    child_label = child.display_name if child and child.display_name else (child.first_name if child else "your child")
    memory_clear_captcha = _memory_clear_captcha(context, child_name=child_label) if memory_user_id else None
    memory_clear_status = str(query_params.get("memory_store_status") or "").strip().lower()
    memory_clear_error = str(query_params.get("memory_store_error") or "").strip().lower()
    memory_clear_alert = None
    if memory_clear_status == "cleared":
        memory_clear_alert = {
            "tone": "success",
            "title": "Memory store cleared",
            "message": f"Resona's saved memory store for {child_label} has been permanently removed.",
        }
    elif memory_clear_error:
        messages = {
            "csrf": "We couldn't verify that request. Refresh and try again.",
            "confirmation": "The confirmation phrase didn't match exactly, so nothing was deleted.",
            "captcha": "The CAPTCHA answer was incorrect, so the memory store was left untouched.",
            "missing": "There isn't a linked memory profile to clear yet.",
        }
        memory_clear_alert = {
            "tone": "danger",
            "title": "Memory store not cleared",
            "message": messages.get(memory_clear_error, "We couldn't clear the memory store right now."),
        }

    memory_payload = {
        "csrf_token": context.csrf_token,
        "view": view,
        "child_name": child_label,
        "graph_url": "/app/memories/daily-routine-data" if view == "routine" else "/app/memories/graph-data",
        "recent_list_url": "/app/memories/recent-list",
        "detail_base_url": "/app/memories",
        "library_url": "/app/memories/library",
        "map_url": "/app/memories/map",
        "routine_url": "/app/memories/daily-routine",
        "resume_url": request.url.path,
        "show_archived": include_archived,
        "search": search,
        "type_filter": type_filter,
        "selected_node_id": selected_node_id,
        "branch_node_id": branch_node_id,
        "show_similarity": show_similarity,
        "recent_page": recent_page,
        "recent_page_total": recent_page_total,
        "recent_changes_url": "/app/memories/recent-changes",
    }
    return _portal_response(
        request,
        "portal/memories.html",
        active_nav=(
            "memories-map"
            if view == "map"
            else "memories-routine"
            if view == "routine"
            else "memories-library"
        ),
        customer_user=context.customer_user,
        child=child,
        memory_view=view,
        memory_payload=memory_payload,
        memory_items=[_memory_row_payload(item) for item in memory_items],
        memory_filter_options=_memory_filter_options(),
        memory_entity_kind_options=_memory_entity_kind_options(),
        memory_facet_options=_memory_facet_options(),
        memory_total=len(memory_items),
        recent_memory_items=[_memory_row_payload(item) for item in recent_memory_items],
        recent_memory_total=recent_memory_total,
        recent_memory_page=recent_page,
        recent_memory_page_total=recent_page_total,
        recent_memory_prev_url=recent_prev_url,
        recent_memory_next_url=recent_next_url,
        show_archived=include_archived,
        search_query=search,
        type_filter=type_filter,
        memory_branch_context=memory_branch_context,
        memory_clear_form={
            "action_url": "/app/memories/clear-store",
            "current_url": current_url,
            "confirmation_phrase": _memory_store_confirmation_phrase(child_label),
            "captcha_question": memory_clear_captcha["question"] if memory_clear_captcha else "",
            "captcha_token": memory_clear_captcha["token"] if memory_clear_captcha else "",
        },
        memory_clear_alert=memory_clear_alert,
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


@router.get("/memories/daily-routine")
async def portal_memories_daily_routine(
    request: Request,
    session: AsyncSession = Depends(get_db_session),
    context: PortalRequestContext = Depends(require_portal_context),
):
    return await _render_portal_memories_page(request, session, context, view="routine")


@router.get("/memories/graph-data")
async def portal_memories_graph_data(
    request: Request,
    session: AsyncSession = Depends(get_db_session),
    context: PortalRequestContext = Depends(require_portal_context),
):
    query_state = _memory_graph_query_state(request)
    child = await _portal_child_profile(request, session, account_id=context.customer_user.account_id)
    if child is None or child.companion_user_id is None:
        return JSONResponse(
            {
                "ok": True,
                "nodes": [],
                "structural_edges": [],
                "similarity_edges": [],
                "query": query_state["search"],
                "selected_node_id": query_state["selected_node_id"],
                "branch_node_id": query_state["branch_node_id"],
                "show_similarity": query_state["show_similarity"],
            }
        )
    graph = await context.container.memory_service.graph_snapshot(
        session,
        user_id=child.companion_user_id,
        include_archived=bool(query_state["include_archived"]),
    )
    await session.commit()
    return JSONResponse(
        {
            "ok": True,
            "nodes": [node.model_dump(mode="json") for node in graph.nodes],
            "structural_edges": [edge.model_dump(mode="json") for edge in graph.structural_edges],
            "similarity_edges": [
                edge.model_dump(mode="json")
                for edge in (graph.similarity_edges if query_state["show_similarity"] else [])
            ],
            "query": query_state["search"],
            "selected_node_id": query_state["selected_node_id"],
            "branch_node_id": query_state["branch_node_id"],
            "show_similarity": query_state["show_similarity"],
        }
    )


@router.get("/memories/daily-routine-data")
async def portal_memories_daily_routine_data(
    request: Request,
    session: AsyncSession = Depends(get_db_session),
    context: PortalRequestContext = Depends(require_portal_context),
):
    query_state = _memory_graph_query_state(request)
    child = await _portal_child_profile(request, session, account_id=context.customer_user.account_id)
    if child is None or child.companion_user_id is None:
        return JSONResponse(
            {
                "ok": True,
                "nodes": [],
                "structural_edges": [],
                "similarity_edges": [],
                "query": query_state["search"],
                "selected_node_id": query_state["selected_node_id"],
                "branch_node_id": query_state["branch_node_id"],
                "show_similarity": query_state["show_similarity"],
            }
        )
    graph = await context.container.memory_service.routine_graph_snapshot(
        session,
        user_id=child.companion_user_id,
        include_archived=bool(query_state["include_archived"]),
    )
    await session.commit()
    return JSONResponse(
        {
            "ok": True,
            "nodes": [node.model_dump(mode="json") for node in graph.nodes],
            "structural_edges": [edge.model_dump(mode="json") for edge in graph.structural_edges],
            "similarity_edges": [
                edge.model_dump(mode="json")
                for edge in (graph.similarity_edges if query_state["show_similarity"] else [])
            ],
            "query": query_state["search"],
            "selected_node_id": query_state["selected_node_id"],
            "branch_node_id": query_state["branch_node_id"],
            "show_similarity": query_state["show_similarity"],
        }
    )


@router.get("/memories/recent-changes")
async def portal_memories_recent_changes(
    request: Request,
    session: AsyncSession = Depends(get_db_session),
    context: PortalRequestContext = Depends(require_portal_context),
):
    child = await _portal_child_profile(request, session, account_id=context.customer_user.account_id)
    if child is None or child.companion_user_id is None:
        return JSONResponse({"ok": True, "changes": []})
    changes = await context.container.memory_service.recent_changes(
        session,
        user_id=child.companion_user_id,
        limit=14,
    )
    await session.commit()
    return JSONResponse(
        {
            "ok": True,
            "changes": [item.model_dump(mode="json") for item in changes],
        }
    )


@router.get("/memories/recent-list")
async def portal_memories_recent_list(
    request: Request,
    session: AsyncSession = Depends(get_db_session),
    context: PortalRequestContext = Depends(require_portal_context),
):
    child = await _portal_child_profile(request, session, account_id=context.customer_user.account_id)
    if child is None or child.companion_user_id is None:
        return JSONResponse(
            {
                "ok": True,
                "items": [],
                "total": 0,
                "page": 1,
                "page_total": 1,
                "has_prev": False,
                "has_next": False,
            }
        )

    view = str(request.query_params.get("view") or "map").strip().lower()
    if view not in {"map", "routine"}:
        view = "map"
    include_archived = _memory_truthy(request.query_params.get("archived"))
    page = _memory_recent_page(request.query_params.get("page"))
    page_size = 5
    world_section = "daily_routine" if view == "routine" else "memories"
    items, total = await context.container.memory_service.list_recent_memories_for_user(
        session,
        user_id=child.companion_user_id,
        include_archived=include_archived,
        world_section=world_section,
        page=page,
        page_size=page_size,
    )
    page_total = max((total + page_size - 1) // page_size, 1)
    if total and page > page_total:
        page = page_total
        items, total = await context.container.memory_service.list_recent_memories_for_user(
            session,
            user_id=child.companion_user_id,
            include_archived=include_archived,
            world_section=world_section,
            page=page,
            page_size=page_size,
        )
    await session.commit()
    return JSONResponse(
        {
            "ok": True,
            "items": [_memory_row_payload(item) for item in items],
            "total": total,
            "page": page,
            "page_total": page_total,
            "has_prev": page > 1,
            "has_next": page < page_total,
        }
    )


@router.post("/memories/clear-store")
async def portal_memories_clear_store(
    request: Request,
    session: AsyncSession = Depends(get_db_session),
    context: PortalRequestContext = Depends(require_portal_context),
):
    form = await request.form()
    csrf_token = str(form.get("csrf_token", ""))
    next_url = _safe_resume_path(str(form.get("next", "")) or "/app/memories/library", default="/app/memories/library")
    if not csrf_token or csrf_token != context.csrf_token:
        return RedirectResponse(
            url=_append_query_params(next_url, memory_store_error="csrf"),
            status_code=303,
        )

    child = await _portal_child_profile(request, session, account_id=context.customer_user.account_id)
    if child is None or child.companion_user_id is None:
        return RedirectResponse(
            url=_append_query_params(next_url, memory_store_error="missing"),
            status_code=303,
        )

    child_label = child.display_name or child.first_name or "your child"
    confirmation_phrase = _memory_store_confirmation_phrase(child_label)
    provided_confirmation = _normalize_confirmation_value(str(form.get("confirmation_text", "")))
    if not hmac.compare_digest(provided_confirmation, _normalize_confirmation_value(confirmation_phrase)):
        return RedirectResponse(
            url=_append_query_params(next_url, memory_store_error="confirmation"),
            status_code=303,
        )

    if not _memory_clear_captcha_valid(
        str(form.get("captcha_token", "")),
        str(form.get("captcha_answer", "")),
        context=context,
        child_name=child_label,
    ):
        return RedirectResponse(
            url=_append_query_params(next_url, memory_store_error="captcha"),
            status_code=303,
        )

    cleared = await context.container.memory_service.clear_memory_store(
        session,
        user_id=child.companion_user_id,
    )
    await session.commit()
    logger.info(
        "portal memory store cleared",
        extra={
            "account_id": str(context.customer_user.account_id),
            "customer_user_id": str(context.customer_user.id),
            "child_profile_id": str(child.id),
            "companion_user_id": str(child.companion_user_id),
            "deleted_memories": cleared.get("deleted_memories", 0),
            "deleted_entities": cleared.get("deleted_entities", 0),
        },
    )
    return RedirectResponse(
        url=_append_query_params(next_url, memory_store_status="cleared", memory_store_error=None),
        status_code=303,
    )


@router.get("/memories/{memory_id}")
async def portal_memory_detail(
    memory_id: str,
    request: Request,
    session: AsyncSession = Depends(get_db_session),
    context: PortalRequestContext = Depends(require_portal_context),
):
    child = await _portal_child_profile(request, session, account_id=context.customer_user.account_id)
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
    child = await _portal_child_profile(request, session, account_id=context.customer_user.account_id)
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
    child = await _portal_child_profile(request, session, account_id=context.customer_user.account_id)
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
    child = await _portal_child_profile(request, session, account_id=context.customer_user.account_id)
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
    child = await _portal_child_profile(request, session, account_id=context.customer_user.account_id)
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
):
    return RedirectResponse(url="/app/child", status_code=303)


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
