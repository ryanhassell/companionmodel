from __future__ import annotations

import json
from datetime import datetime
from pathlib import Path

from fastapi import APIRouter, Depends, Request
from fastapi.responses import RedirectResponse
from sqlalchemy import delete, desc, func, or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.admin.dependencies import AdminRequestContext, get_optional_admin_context, verify_csrf_or_403
from app.core.templating import templates
from app.db.session import get_db_session
from app.models.admin import JobRun
from app.models.communication import CallRecord, Conversation, DeliveryAttempt, MediaAsset, Message, SafetyEvent
from app.models.configuration import AppSetting, PromptTemplate, ScheduleRule
from app.models.enums import AppSettingScope, Channel, DeliveryStatus, Direction, MemoryType, MessageStatus, ScheduleRuleType
from app.models.memory import MemoryItem
from app.models.persona import Persona
from app.models.user import User
from app.utils.text import make_idempotency_key
from app.utils.time import parse_clock
from app.utils.time import utc_now

router = APIRouter(prefix="/admin", tags=["admin"])
REALTIME_VOICE_OPTIONS = [
    ("", "Use app default"),
    ("alloy", "Alloy"),
    ("ash", "Ash"),
    ("ballad", "Ballad"),
    ("cedar", "Cedar"),
    ("coral", "Coral"),
    ("echo", "Echo"),
    ("marin", "Marin"),
    ("sage", "Sage"),
    ("shimmer", "Shimmer"),
    ("verse", "Verse"),
]


def _redirect_login() -> RedirectResponse:
    return RedirectResponse(url="/login", status_code=303)


def _context_dict(request: Request, context: AdminRequestContext, *, active_nav: str, **extra):
    base = {
        "request": request,
        "admin_user": context.admin_user,
        "csrf_token": context.csrf_token,
        "active_nav": active_nav,
        "settings_summary": context.container.settings.redacted(),
    }
    base.update(extra)
    return base


def _split_csv(value: str | None) -> list[str]:
    if not value:
        return []
    return [item.strip() for item in value.split(",") if item.strip()]


def _parse_json_input(value: str | None, default):
    if not value:
        return default
    try:
        return json.loads(value)
    except json.JSONDecodeError:
        return default


async def _persona_reference_assets(session: AsyncSession, personas: list[Persona]) -> dict[str, MediaAsset]:
    assets: dict[str, MediaAsset] = {}
    for persona in personas:
        asset_id = (persona.visual_bible or {}).get("reference_image_asset_id")
        if not asset_id:
            continue
        asset = await session.get(MediaAsset, asset_id)
        if asset is not None:
            assets[str(persona.id)] = asset
    return assets


async def _message_assets(session: AsyncSession, messages: list[Message]) -> dict[str, list[MediaAsset]]:
    message_ids = [message.id for message in messages]
    if not message_ids:
        return {}
    assets = (
        await session.execute(
            select(MediaAsset)
            .where(MediaAsset.message_id.in_(message_ids))
            .order_by(MediaAsset.created_at)
        )
    ).scalars().all()
    grouped: dict[str, list[MediaAsset]] = {}
    for asset in assets:
        if asset.message_id is None:
            continue
        grouped.setdefault(str(asset.message_id), []).append(asset)
    return grouped


async def _has_pending_media(session: AsyncSession, user: User | None) -> bool:
    if user is None:
        return False
    pending = await session.scalar(
        select(func.count())
        .select_from(MediaAsset)
        .where(
            MediaAsset.user_id == user.id,
            MediaAsset.generation_status == "processing",
        )
    )
    return bool(pending)


@router.get("")
async def overview(
    request: Request,
    session: AsyncSession = Depends(get_db_session),
    context: AdminRequestContext | None = Depends(get_optional_admin_context),
):
    if context is None:
        return _redirect_login()
    counts = {
        "users": int((await session.scalar(select(func.count()).select_from(User))) or 0),
        "personas": int((await session.scalar(select(func.count()).select_from(Persona))) or 0),
        "messages": int((await session.scalar(select(func.count()).select_from(Message))) or 0),
        "memories": int((await session.scalar(select(func.count()).select_from(MemoryItem))) or 0),
        "safety_events": int((await session.scalar(select(func.count()).select_from(SafetyEvent))) or 0),
    }
    latest_jobs = (await session.execute(select(JobRun).order_by(desc(JobRun.created_at)).limit(10))).scalars().all()
    return templates.TemplateResponse(
        "admin/overview.html",
        _context_dict(request, context, active_nav="overview", counts=counts, latest_jobs=latest_jobs),
    )


@router.get("/users")
async def users_page(
    request: Request,
    session: AsyncSession = Depends(get_db_session),
    context: AdminRequestContext | None = Depends(get_optional_admin_context),
):
    if context is None:
        return _redirect_login()
    users = (await session.execute(select(User).order_by(desc(User.updated_at)).limit(100))).scalars().all()
    personas = (await session.execute(select(Persona).order_by(Persona.display_name))).scalars().all()
    return templates.TemplateResponse(
        "admin/users.html",
        _context_dict(request, context, active_nav="users", users=users, personas=personas),
    )


@router.post("/users")
async def users_submit(
    request: Request,
    session: AsyncSession = Depends(get_db_session),
    context: AdminRequestContext | None = Depends(get_optional_admin_context),
):
    if context is None:
        return _redirect_login()
    await verify_csrf_or_403(request, context)
    form = await request.form()
    user_id = form.get("user_id") or None
    user = await session.get(User, user_id) if user_id else User(phone_number=str(form.get("phone_number", "")))
    if user_id is None:
        session.add(user)
    user.display_name = str(form.get("display_name") or "") or None
    user.phone_number = str(form.get("phone_number", ""))
    user.timezone = str(form.get("timezone") or "America/New_York")
    user.notes = str(form.get("notes") or "") or None
    user.profile_json = _parse_json_input(str(form.get("profile_json") or ""), {})
    user.schedule_overrides = _parse_json_input(str(form.get("schedule_overrides") or ""), {})
    user.safety_overrides = _parse_json_input(str(form.get("safety_overrides") or ""), {})
    preferred_persona_id = form.get("preferred_persona_id")
    user.preferred_persona_id = preferred_persona_id or None
    user.is_enabled = form.get("is_enabled") == "on"
    await context.container.audit_service.record(
        session,
        admin_user_id=str(context.admin_user.id),
        action="upsert_user",
        entity_type="user",
        entity_id=str(user.id),
        summary=f"Updated user {user.phone_number}",
    )
    await session.commit()
    return RedirectResponse(url="/admin/users", status_code=303)


@router.get("/personas")
async def personas_page(
    request: Request,
    session: AsyncSession = Depends(get_db_session),
    context: AdminRequestContext | None = Depends(get_optional_admin_context),
):
    if context is None:
        return _redirect_login()
    personas = (await session.execute(select(Persona).order_by(desc(Persona.updated_at)))).scalars().all()
    reference_assets = await _persona_reference_assets(session, list(personas))
    return templates.TemplateResponse(
        "admin/personas.html",
        _context_dict(
            request,
            context,
            active_nav="personas",
            personas=personas,
            reference_assets=reference_assets,
            realtime_voice_options=REALTIME_VOICE_OPTIONS,
        ),
    )


@router.post("/personas")
async def personas_submit(
    request: Request,
    session: AsyncSession = Depends(get_db_session),
    context: AdminRequestContext | None = Depends(get_optional_admin_context),
):
    if context is None:
        return _redirect_login()
    await verify_csrf_or_403(request, context)
    form = await request.form()
    action = str(form.get("action") or "upsert_persona")
    if action == "upload_reference":
        persona = await session.get(Persona, form.get("persona_id"))
        upload = form.get("reference_image")
        if persona is not None and getattr(upload, "filename", ""):
            await context.container.image_service.save_persona_reference_image(
                session,
                persona=persona,
                upload=upload,
            )
            await context.container.audit_service.record(
                session,
                admin_user_id=str(context.admin_user.id),
                action="upload_persona_reference_image",
                entity_type="persona",
                entity_id=str(persona.id),
                summary=f"Uploaded reference image for persona {persona.display_name}",
            )
            await session.commit()
        return RedirectResponse(url="/admin/personas", status_code=303)

    persona_id = form.get("persona_id") or None
    persona = await session.get(Persona, persona_id) if persona_id else Persona(key=str(form.get("key", "")), display_name=str(form.get("display_name", "")))
    if persona_id is None:
        session.add(persona)
    persona.key = str(form.get("key", ""))
    persona.display_name = str(form.get("display_name", ""))
    persona.description = str(form.get("description") or "") or None
    persona.style = str(form.get("style") or "") or None
    persona.tone = str(form.get("tone") or "") or None
    persona.boundaries = str(form.get("boundaries") or "") or None
    persona.topics_of_interest = _split_csv(str(form.get("topics_of_interest") or ""))
    persona.favorite_activities = _split_csv(str(form.get("favorite_activities") or ""))
    persona.image_appearance = str(form.get("image_appearance") or "") or None
    persona.speech_style = str(form.get("speech_style") or "") or None
    persona.disclosure_policy = str(form.get("disclosure_policy") or "") or None
    persona.texting_length_preference = str(form.get("texting_length_preference") or "") or None
    persona.emoji_tendency = str(form.get("emoji_tendency") or "") or None
    persona.proactive_outreach_style = str(form.get("proactive_outreach_style") or "") or None
    persona.visual_bible = _parse_json_input(str(form.get("visual_bible") or ""), {})
    persona.prompt_overrides = _parse_json_input(str(form.get("prompt_overrides") or ""), {})
    calling_numbers = _split_csv(str(form.get("calling_numbers") or ""))
    if calling_numbers:
        persona.prompt_overrides["calling_numbers"] = calling_numbers
    else:
        persona.prompt_overrides.pop("calling_numbers", None)
    elevenlabs_voice_id = str(form.get("elevenlabs_voice_id") or "").strip()
    if elevenlabs_voice_id:
        persona.prompt_overrides["elevenlabs_voice_id"] = elevenlabs_voice_id
    else:
        persona.prompt_overrides.pop("elevenlabs_voice_id", None)
    realtime_voice = str(form.get("realtime_voice") or "").strip()
    if realtime_voice:
        persona.prompt_overrides["realtime_voice"] = realtime_voice
    else:
        persona.prompt_overrides.pop("realtime_voice", None)
    persona.safety_overrides = _parse_json_input(str(form.get("safety_overrides") or ""), {})
    persona.operator_notes = str(form.get("operator_notes") or "") or None
    persona.is_active = form.get("is_active") == "on"
    if persona.is_active:
        others = (await session.execute(select(Persona).where(Persona.id != persona.id))).scalars().all()
        for other in others:
            other.is_active = False
    await context.container.audit_service.record(
        session,
        admin_user_id=str(context.admin_user.id),
        action="upsert_persona",
        entity_type="persona",
        entity_id=str(persona.id),
        summary=f"Updated persona {persona.display_name}",
    )
    await session.commit()
    return RedirectResponse(url="/admin/personas", status_code=303)


@router.get("/conversations")
async def conversations_page(
    request: Request,
    session: AsyncSession = Depends(get_db_session),
    context: AdminRequestContext | None = Depends(get_optional_admin_context),
):
    if context is None:
        return _redirect_login()
    conversations = (await session.execute(select(Conversation).order_by(desc(Conversation.updated_at)).limit(100))).scalars().all()
    return templates.TemplateResponse(
        "admin/conversations.html",
        _context_dict(request, context, active_nav="conversations", conversations=conversations),
    )


@router.get("/messages")
async def messages_page(
    request: Request,
    session: AsyncSession = Depends(get_db_session),
    context: AdminRequestContext | None = Depends(get_optional_admin_context),
):
    if context is None:
        return _redirect_login()
    messages = (await session.execute(select(Message).order_by(desc(Message.created_at)).limit(200))).scalars().all()
    return templates.TemplateResponse(
        "admin/messages.html",
        _context_dict(request, context, active_nav="messages", messages=messages),
    )


@router.get("/memory")
async def memory_page(
    request: Request,
    session: AsyncSession = Depends(get_db_session),
    context: AdminRequestContext | None = Depends(get_optional_admin_context),
):
    if context is None:
        return _redirect_login()
    memory_items = (await session.execute(select(MemoryItem).order_by(desc(MemoryItem.updated_at)).limit(150))).scalars().all()
    users = (await session.execute(select(User).order_by(User.phone_number))).scalars().all()
    personas = (await session.execute(select(Persona).order_by(Persona.display_name))).scalars().all()
    return templates.TemplateResponse(
        "admin/memory.html",
        _context_dict(request, context, active_nav="memory", memory_items=memory_items, users=users, personas=personas, memory_types=list(MemoryType)),
    )


@router.post("/memory")
async def memory_submit(
    request: Request,
    session: AsyncSession = Depends(get_db_session),
    context: AdminRequestContext | None = Depends(get_optional_admin_context),
):
    if context is None:
        return _redirect_login()
    await verify_csrf_or_403(request, context)
    form = await request.form()
    memory_id = form.get("memory_id")
    item = await session.get(MemoryItem, memory_id) if memory_id else MemoryItem(memory_type=MemoryType.fact, content="")
    if memory_id is None:
        session.add(item)
    item.user_id = form.get("user_id") or None
    item.persona_id = form.get("persona_id") or None
    item.memory_type = MemoryType(str(form.get("memory_type") or "fact"))
    item.title = str(form.get("title") or "") or None
    item.content = str(form.get("content") or "")
    item.summary = str(form.get("summary") or "") or None
    item.tags = _split_csv(str(form.get("tags") or ""))
    item.importance_score = float(form.get("importance_score") or 0.5)
    item.pinned = form.get("pinned") == "on"
    item.disabled = form.get("disabled") == "on"
    item.metadata_json = _parse_json_input(str(form.get("metadata_json") or ""), {})
    await session.flush()
    config = await context.container.config_service.get_effective_config(session)
    await context.container.memory_service.embed_items(session, [item], config=config)
    await session.commit()
    return RedirectResponse(url="/admin/memory", status_code=303)


@router.get("/vector-search")
async def vector_search_page(
    request: Request,
    query: str | None = None,
    user_id: str | None = None,
    persona_id: str | None = None,
    session: AsyncSession = Depends(get_db_session),
    context: AdminRequestContext | None = Depends(get_optional_admin_context),
):
    if context is None:
        return _redirect_login()
    users = (await session.execute(select(User).order_by(User.phone_number))).scalars().all()
    personas = (await session.execute(select(Persona).order_by(Persona.display_name))).scalars().all()
    results = []
    if query and user_id:
        results = await context.container.memory_service.retrieve(
            session,
            user_id=user_id,
            persona_id=persona_id,
            query=query,
            top_k=context.container.settings.memory.top_k,
            threshold=context.container.settings.memory.similarity_threshold,
        )
    return templates.TemplateResponse(
        "admin/vector_search.html",
        _context_dict(request, context, active_nav="vector-search", query=query, users=users, personas=personas, results=results),
    )


@router.get("/schedules")
async def schedules_page(
    request: Request,
    session: AsyncSession = Depends(get_db_session),
    context: AdminRequestContext | None = Depends(get_optional_admin_context),
):
    if context is None:
        return _redirect_login()
    rules = (await session.execute(select(ScheduleRule).order_by(desc(ScheduleRule.updated_at)).limit(120))).scalars().all()
    users = (await session.execute(select(User).order_by(User.phone_number))).scalars().all()
    personas = (await session.execute(select(Persona).order_by(Persona.display_name))).scalars().all()
    return templates.TemplateResponse(
        "admin/schedules.html",
        _context_dict(request, context, active_nav="schedules", rules=rules, users=users, personas=personas, rule_types=list(ScheduleRuleType)),
    )


@router.post("/schedules")
async def schedules_submit(
    request: Request,
    session: AsyncSession = Depends(get_db_session),
    context: AdminRequestContext | None = Depends(get_optional_admin_context),
):
    if context is None:
        return _redirect_login()
    await verify_csrf_or_403(request, context)
    form = await request.form()
    rule = ScheduleRule(
        user_id=form.get("user_id") or None,
        persona_id=form.get("persona_id") or None,
        name=str(form.get("name", "")),
        rule_type=ScheduleRuleType(str(form.get("rule_type") or "proactive_window")),
        weekday=int(form.get("weekday")) if form.get("weekday") else None,
        start_time=parse_clock(str(form.get("start_time"))) if form.get("start_time") else None,
        end_time=parse_clock(str(form.get("end_time"))) if form.get("end_time") else None,
        min_gap_minutes=int(form.get("min_gap_minutes")) if form.get("min_gap_minutes") else None,
        max_gap_minutes=int(form.get("max_gap_minutes")) if form.get("max_gap_minutes") else None,
        probability=float(form.get("probability")) if form.get("probability") else None,
        config_json=_parse_json_input(str(form.get("config_json") or ""), {}),
        enabled=form.get("enabled") == "on",
    )
    session.add(rule)
    await session.commit()
    return RedirectResponse(url="/admin/schedules", status_code=303)


@router.get("/prompts")
async def prompts_page(
    request: Request,
    session: AsyncSession = Depends(get_db_session),
    context: AdminRequestContext | None = Depends(get_optional_admin_context),
):
    if context is None:
        return _redirect_login()
    prompts = (await session.execute(select(PromptTemplate).order_by(PromptTemplate.name, desc(PromptTemplate.version)))).scalars().all()
    return templates.TemplateResponse(
        "admin/prompts.html",
        _context_dict(request, context, active_nav="prompts", prompts=prompts),
    )


@router.post("/prompts")
async def prompts_submit(
    request: Request,
    session: AsyncSession = Depends(get_db_session),
    context: AdminRequestContext | None = Depends(get_optional_admin_context),
):
    if context is None:
        return _redirect_login()
    await verify_csrf_or_403(request, context)
    form = await request.form()
    name = str(form.get("name", ""))
    existing = (await session.execute(select(PromptTemplate).where(PromptTemplate.name == name).order_by(desc(PromptTemplate.version)))).scalars().first()
    if existing:
        existing.is_active = False
        version = existing.version + 1
    else:
        version = 1
    prompt = PromptTemplate(
        name=name,
        channel=str(form.get("channel") or "sms"),
        description=str(form.get("description") or "") or None,
        version=version,
        body=str(form.get("body") or ""),
        variables_json=_split_csv(str(form.get("variables_json") or "")),
        source="admin",
        is_active=True,
        locked=form.get("locked") == "on",
    )
    session.add(prompt)
    await session.commit()
    return RedirectResponse(url="/admin/prompts", status_code=303)


@router.get("/safety-events")
async def safety_events_page(
    request: Request,
    session: AsyncSession = Depends(get_db_session),
    context: AdminRequestContext | None = Depends(get_optional_admin_context),
):
    if context is None:
        return _redirect_login()
    events = (await session.execute(select(SafetyEvent).order_by(desc(SafetyEvent.created_at)).limit(100))).scalars().all()
    return templates.TemplateResponse(
        "admin/safety.html",
        _context_dict(request, context, active_nav="safety", events=events),
    )


@router.get("/delivery-failures")
async def delivery_failures_page(
    request: Request,
    session: AsyncSession = Depends(get_db_session),
    context: AdminRequestContext | None = Depends(get_optional_admin_context),
):
    if context is None:
        return _redirect_login()
    attempts = (
        await session.execute(
            select(DeliveryAttempt)
            .where(or_(DeliveryAttempt.status == DeliveryStatus.failed, DeliveryAttempt.error_message.is_not(None)))
            .order_by(desc(DeliveryAttempt.created_at))
            .limit(100)
        )
    ).scalars().all()
    return templates.TemplateResponse(
        "admin/deliveries.html",
        _context_dict(request, context, active_nav="deliveries", attempts=attempts),
    )


@router.get("/media")
async def media_page(
    request: Request,
    session: AsyncSession = Depends(get_db_session),
    context: AdminRequestContext | None = Depends(get_optional_admin_context),
):
    if context is None:
        return _redirect_login()
    assets = (await session.execute(select(MediaAsset).order_by(desc(MediaAsset.created_at)).limit(100))).scalars().all()
    users = (await session.execute(select(User).order_by(User.phone_number))).scalars().all()
    personas = (await session.execute(select(Persona).order_by(Persona.display_name))).scalars().all()
    return templates.TemplateResponse(
        "admin/media.html",
        _context_dict(request, context, active_nav="media", assets=assets, users=users, personas=personas),
    )


@router.post("/media")
async def media_submit(
    request: Request,
    session: AsyncSession = Depends(get_db_session),
    context: AdminRequestContext | None = Depends(get_optional_admin_context),
):
    if context is None:
        return _redirect_login()
    await verify_csrf_or_403(request, context)
    form = await request.form()
    persona = await session.get(Persona, form.get("persona_id"))
    user = await session.get(User, form.get("user_id")) if form.get("user_id") else None
    if persona:
        config = await context.container.config_service.get_effective_config(session, user=user, persona=persona)
        await context.container.image_service.generate_image(
            session,
            persona=persona,
            user=user,
            scene_hint=str(form.get("scene_hint") or ""),
            negative_prompt=str(form.get("negative_prompt") or "") or None,
            metadata={"source": "admin"},
            config=config,
        )
        await session.commit()
    return RedirectResponse(url="/admin/media", status_code=303)


@router.get("/test-tools")
async def test_tools_page(
    request: Request,
    session: AsyncSession = Depends(get_db_session),
    context: AdminRequestContext | None = Depends(get_optional_admin_context),
):
    if context is None:
        return _redirect_login()
    users = (await session.execute(select(User).order_by(User.phone_number))).scalars().all()
    personas = (await session.execute(select(Persona).order_by(Persona.display_name))).scalars().all()
    return templates.TemplateResponse(
        "admin/test_tools.html",
        _context_dict(request, context, active_nav="test-tools", users=users, personas=personas),
    )


@router.post("/test-tools")
async def test_tools_submit(
    request: Request,
    session: AsyncSession = Depends(get_db_session),
    context: AdminRequestContext | None = Depends(get_optional_admin_context),
):
    if context is None:
        return _redirect_login()
    await verify_csrf_or_403(request, context)
    form = await request.form()
    action = str(form.get("action") or "")
    if action == "send_message":
        user = await session.get(User, form.get("user_id"))
        persona = await session.get(Persona, form.get("persona_id")) if form.get("persona_id") else await context.container.conversation_service.get_active_persona(session, user)
        conversation = await context.container.conversation_service.get_or_create_conversation(session, user=user, persona=persona)
        await context.container.message_service.send_outbound_message(
            session,
            user=user,
            persona=persona,
            conversation=conversation,
            body=str(form.get("body") or ""),
            is_proactive=False,
            ignore_quiet_hours=True,
        )
    elif action == "prompt_preview":
        persona = await session.get(Persona, form.get("persona_id")) if form.get("persona_id") else None
        user = await session.get(User, form.get("user_id")) if form.get("user_id") else None
        config = await context.container.config_service.get_effective_config(session, user=user, persona=persona)
        rendered = await context.container.prompt_service.render(
            session,
            str(form.get("template_name") or "reactive_reply"),
            {"user": user, "persona": persona, "config": config, "recent_messages": [], "memory_hits": []},
        )
        return templates.TemplateResponse(
            "admin/test_tools.html",
            _context_dict(
                request,
                context,
                active_nav="test-tools",
                users=(await session.execute(select(User).order_by(User.phone_number))).scalars().all(),
                personas=(await session.execute(select(Persona).order_by(Persona.display_name))).scalars().all(),
                preview_text=rendered,
            ),
        )
    elif action == "manual_proactive":
        await context.container.proactive_service.trigger_for_user(session, user_id=form.get("user_id"))
    elif action == "test_call":
        user = await session.get(User, form.get("user_id"))
        persona = await session.get(Persona, form.get("persona_id")) if form.get("persona_id") else await context.container.conversation_service.get_active_persona(session, user)
        config = await context.container.config_service.get_effective_config(session, user=user, persona=persona)
        await context.container.voice_service.initiate_call(
            session,
            user=user,
            persona=persona,
            config=config,
            opening_line=str(form.get("opening_line") or "") or None,
        )
    await session.commit()
    return RedirectResponse(url="/admin/test-tools", status_code=303)


@router.get("/simulator")
async def simulator_page(
    request: Request,
    session: AsyncSession = Depends(get_db_session),
    context: AdminRequestContext | None = Depends(get_optional_admin_context),
):
    if context is None:
        return _redirect_login()
    users = (await session.execute(select(User).order_by(User.phone_number))).scalars().all()
    personas = (await session.execute(select(Persona).order_by(Persona.display_name))).scalars().all()
    selected_user_id = request.query_params.get("user_id")
    selected_user = await session.get(User, selected_user_id) if selected_user_id else None
    selected_persona = None
    thread_messages: list[Message] = []
    message_assets: dict[str, list[MediaAsset]] = {}
    flash_error = request.query_params.get("error")
    pending_media = False
    if selected_user:
        selected_persona = await context.container.conversation_service.get_active_persona(session, selected_user)
        conversation = await context.container.conversation_service.get_or_create_conversation(
            session,
            user=selected_user,
            persona=selected_persona,
        )
        thread_messages = await context.container.conversation_service.recent_messages(
            session,
            conversation_id=conversation.id,
            limit=30,
        )
        message_assets = await _message_assets(session, thread_messages)
        pending_media = await _has_pending_media(session, selected_user)
    return templates.TemplateResponse(
        "admin/simulator.html",
        _context_dict(
            request,
            context,
            active_nav="simulator",
            users=users,
            personas=personas,
            selected_user=selected_user,
            selected_persona=selected_persona,
            thread_messages=thread_messages,
            message_assets=message_assets,
            flash_error=flash_error,
            pending_media=pending_media,
        ),
    )


@router.post("/simulator")
async def simulator_submit(
    request: Request,
    session: AsyncSession = Depends(get_db_session),
    context: AdminRequestContext | None = Depends(get_optional_admin_context),
):
    if context is None:
        return _redirect_login()
    await verify_csrf_or_403(request, context)
    form = await request.form()
    user_id = form.get("user_id")
    if not user_id:
        return RedirectResponse(url="/admin/simulator", status_code=303)
    user = await session.get(User, user_id)
    if user is None:
        return RedirectResponse(url="/admin/simulator", status_code=303)
    persona = await session.get(Persona, form.get("persona_id")) if form.get("persona_id") else None
    action = str(form.get("action") or "simulate_text")
    if action == "clear_thread":
        conversation = await context.container.conversation_service.get_or_create_conversation(
            session,
            user=user,
            persona=persona or await context.container.conversation_service.get_active_persona(session, user),
        )
        message_ids = (
            await session.execute(select(Message.id).where(Message.conversation_id == conversation.id))
        ).scalars().all()
        if message_ids:
            memory_ids = (
                await session.execute(select(MemoryItem.id).where(MemoryItem.source_message_id.in_(message_ids)))
            ).scalars().all()
            if memory_ids:
                await session.execute(delete(MemoryItem).where(MemoryItem.consolidated_into_id.in_(memory_ids)))
                await session.execute(delete(MemoryItem).where(MemoryItem.id.in_(memory_ids)))
            await session.execute(delete(MediaAsset).where(MediaAsset.message_id.in_(message_ids)))
            await session.execute(delete(DeliveryAttempt).where(DeliveryAttempt.message_id.in_(message_ids)))
            await session.execute(delete(SafetyEvent).where(SafetyEvent.message_id.in_(message_ids)))
            await session.execute(delete(Message).where(Message.id.in_(message_ids)))
        await session.commit()
        return RedirectResponse(url=f"/admin/simulator?user_id={user.id}", status_code=303)
    if action == "simulate_image":
        persona = persona or await context.container.conversation_service.get_active_persona(session, user)
        if persona is not None:
            scene_hint = str(form.get("scene_hint") or "").strip()
            if scene_hint:
                config = await context.container.config_service.get_effective_config(session, user=user, persona=persona)
                conversation = await context.container.conversation_service.get_or_create_conversation(
                    session,
                    user=user,
                    persona=persona,
                )
                asset = await context.container.image_service.generate_image(
                    session,
                    persona=persona,
                    user=user,
                    scene_hint=scene_hint,
                    negative_prompt=str(form.get("negative_prompt") or "") or None,
                    config=config,
                    metadata={"source": "admin_simulator"},
                )
                message = Message(
                    conversation_id=conversation.id,
                    user_id=user.id,
                    persona_id=persona.id if persona else None,
                    direction=Direction.outbound,
                    channel=Channel.mms,
                    provider="simulator",
                    idempotency_key=make_idempotency_key("simulator-image", conversation.id, scene_hint, utc_now()),
                    body=str(form.get("caption") or "").strip() or None,
                    normalized_body=None,
                    status=MessageStatus.sent,
                    is_proactive=False,
                    sent_at=utc_now(),
                    metadata_json={"source": "admin_simulator", "scene_hint": scene_hint},
                )
                session.add(message)
                await session.flush()
                asset.message_id = message.id
                context.container.conversation_service.mark_outbound(user, conversation)
                await session.commit()
                if asset.generation_status != "ready":
                    return RedirectResponse(
                        url=f"/admin/simulator?user_id={user.id}&error=image_failed",
                        status_code=303,
                    )
        return RedirectResponse(url=f"/admin/simulator?user_id={user.id}", status_code=303)
    body = str(form.get("body") or "").strip()
    if body:
        await context.container.message_service.simulate_inbound_message(
            session,
            user=user,
            persona=persona,
            body=body,
            force_photo=form.get("force_photo") == "on",
        )
        await session.commit()
    return RedirectResponse(url=f"/admin/simulator?user_id={user.id}", status_code=303)


@router.get("/calls")
async def calls_page(
    request: Request,
    session: AsyncSession = Depends(get_db_session),
    context: AdminRequestContext | None = Depends(get_optional_admin_context),
):
    if context is None:
        return _redirect_login()
    calls = (await session.execute(select(CallRecord).order_by(desc(CallRecord.created_at)).limit(100))).scalars().all()
    return templates.TemplateResponse(
        "admin/calls.html",
        _context_dict(request, context, active_nav="calls", calls=calls),
    )


@router.get("/health")
async def health_page(
    request: Request,
    session: AsyncSession = Depends(get_db_session),
    context: AdminRequestContext | None = Depends(get_optional_admin_context),
):
    if context is None:
        return _redirect_login()
    jobs = (await session.execute(select(JobRun).order_by(desc(JobRun.created_at)).limit(20))).scalars().all()
    return templates.TemplateResponse(
        "admin/health.html",
        _context_dict(
            request,
            context,
            active_nav="health",
            jobs=jobs,
            scheduler_running=context.container.scheduler_service.scheduler.running,
        ),
    )


@router.get("/settings")
async def settings_page(
    request: Request,
    session: AsyncSession = Depends(get_db_session),
    context: AdminRequestContext | None = Depends(get_optional_admin_context),
):
    if context is None:
        return _redirect_login()
    settings_rows = (await session.execute(select(AppSetting).order_by(AppSetting.namespace, AppSetting.key))).scalars().all()
    users = (await session.execute(select(User).order_by(User.phone_number))).scalars().all()
    personas = (await session.execute(select(Persona).order_by(Persona.display_name))).scalars().all()
    return templates.TemplateResponse(
        "admin/settings.html",
        _context_dict(request, context, active_nav="settings", settings_rows=settings_rows, users=users, personas=personas, scopes=list(AppSettingScope)),
    )


@router.post("/settings")
async def settings_submit(
    request: Request,
    session: AsyncSession = Depends(get_db_session),
    context: AdminRequestContext | None = Depends(get_optional_admin_context),
):
    if context is None:
        return _redirect_login()
    await verify_csrf_or_403(request, context)
    form = await request.form()
    await context.container.config_service.upsert_setting(
        session,
        namespace=str(form.get("namespace") or ""),
        key=str(form.get("key") or ""),
        value_json=_parse_json_input(str(form.get("value_json") or ""), str(form.get("value_json") or "")),
        description=str(form.get("description") or "") or None,
        scope=AppSettingScope(str(form.get("scope") or "global")),
        user_id=form.get("user_id") or None,
        persona_id=form.get("persona_id") or None,
    )
    await session.commit()
    return RedirectResponse(url="/admin/settings", status_code=303)


@router.get("/logs")
async def logs_page(
    request: Request,
    context: AdminRequestContext | None = Depends(get_optional_admin_context),
):
    if context is None:
        return _redirect_login()
    log_path = Path(context.container.settings.log_path)
    content = ""
    if log_path.exists():
        lines = log_path.read_text(encoding="utf-8", errors="ignore").splitlines()
        content = "\n".join(lines[-200:])
    return templates.TemplateResponse(
        "admin/logs.html",
        _context_dict(request, context, active_nav="logs", log_content=content),
    )
