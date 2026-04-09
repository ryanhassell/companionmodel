from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.responses import FileResponse, RedirectResponse
from sqlalchemy import desc, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.admin.dependencies import get_container, require_admin_context
from app.db.session import get_db_session
from app.models.admin import JobRun
from app.models.conversation_state import ConversationState
from app.models.communication import MediaAsset, Message
from app.models.configuration import AppSetting
from app.models.enums import AppSettingScope, Direction, JobStatus
from app.models.persona import Persona
from app.models.user import User
from app.schemas.api import AppSettingUpsertRequest, GenerateImageRequest, InitiateCallRequest, MemorySearchRequest, PersonaUpsertRequest, SendMessageRequest
from app.services.container import ServiceContainer

router = APIRouter(prefix="/api", tags=["api"])


@router.get("/media/{asset_id}", response_model=None)
async def media_file(
    asset_id: str,
    session: AsyncSession = Depends(get_db_session),
) -> FileResponse | RedirectResponse:
    asset = await session.get(MediaAsset, asset_id)
    if asset is None:
        raise HTTPException(status_code=404, detail="Asset not found")
    if asset.local_path:
        return FileResponse(asset.local_path, media_type=asset.mime_type)
    if asset.remote_url:
        return RedirectResponse(asset.remote_url)
    raise HTTPException(status_code=404, detail="Asset has no file")


@router.get("/config/effective")
async def effective_config(
    user_id: str | None = None,
    persona_id: str | None = None,
    session: AsyncSession = Depends(get_db_session),
    container: ServiceContainer = Depends(get_container),
    _: object = Depends(require_admin_context),
) -> dict[str, object]:
    user = await session.get(User, user_id) if user_id else None
    persona = await session.get(Persona, persona_id) if persona_id else None
    return await container.config_service.get_effective_config(session, user=user, persona=persona)


@router.post("/messages/send")
async def send_message(
    payload: SendMessageRequest,
    session: AsyncSession = Depends(get_db_session),
    container: ServiceContainer = Depends(get_container),
    _: object = Depends(require_admin_context),
) -> dict[str, str]:
    user = await session.get(User, payload.user_id)
    if user is None:
        raise HTTPException(status_code=404, detail="User not found")
    persona = await session.get(Persona, payload.persona_id) if payload.persona_id else await container.conversation_service.get_active_persona(session, user)
    conversation = await container.conversation_service.get_or_create_conversation(session, user=user, persona=persona)
    assets = []
    if payload.media_asset_ids:
        stmt = select(MediaAsset).where(MediaAsset.id.in_(payload.media_asset_ids))
        assets = list((await session.execute(stmt)).scalars().all())
    message = await container.message_service.send_outbound_message(
        session,
        user=user,
        persona=persona,
        conversation=conversation,
        body=payload.body,
        media_assets=assets,
        is_proactive=payload.is_proactive,
        ignore_quiet_hours=True,
    )
    await session.commit()
    return {"message_id": str(message.id), "status": message.status.value}


@router.post("/proactive/trigger/{user_id}")
async def trigger_proactive(
    user_id: str,
    session: AsyncSession = Depends(get_db_session),
    container: ServiceContainer = Depends(get_container),
    _: object = Depends(require_admin_context),
) -> dict[str, int]:
    triggered = await container.proactive_service.trigger_for_user(session, user_id=user_id)
    await session.commit()
    return {"triggered": triggered}


@router.post("/images/generate")
async def generate_image(
    payload: GenerateImageRequest,
    session: AsyncSession = Depends(get_db_session),
    container: ServiceContainer = Depends(get_container),
    _: object = Depends(require_admin_context),
) -> dict[str, str]:
    persona = await session.get(Persona, payload.persona_id)
    user = await session.get(User, payload.user_id) if payload.user_id else None
    message = await session.get(Message, payload.attach_to_message_id) if payload.attach_to_message_id else None
    if persona is None:
        raise HTTPException(status_code=404, detail="Persona not found")
    config = await container.config_service.get_effective_config(session, user=user, persona=persona)
    asset = await container.image_service.generate_image(
        session,
        persona=persona,
        user=user,
        scene_hint=payload.scene_hint,
        attach_to_message=message,
        negative_prompt=payload.negative_prompt,
        metadata=payload.metadata,
        config=config,
    )
    await session.commit()
    return {"asset_id": str(asset.id), "status": asset.generation_status}


@router.post("/calls/initiate")
async def initiate_call(
    payload: InitiateCallRequest,
    session: AsyncSession = Depends(get_db_session),
    container: ServiceContainer = Depends(get_container),
    _: object = Depends(require_admin_context),
) -> dict[str, str]:
    user = await session.get(User, payload.user_id)
    persona = await session.get(Persona, payload.persona_id) if payload.persona_id else await container.conversation_service.get_active_persona(session, user)
    if user is None:
        raise HTTPException(status_code=404, detail="User not found")
    config = await container.config_service.get_effective_config(session, user=user, persona=persona)
    record = await container.voice_service.initiate_call(
        session,
        user=user,
        persona=persona,
        config=config,
        opening_line=payload.opening_line,
    )
    await session.commit()
    return {
        "call_id": str(record.id),
        "status": record.status.value,
        "transport": str((record.metadata_json or {}).get("transport") or "twilio_twiml"),
    }


@router.post("/memory/search")
async def memory_search(
    payload: MemorySearchRequest,
    session: AsyncSession = Depends(get_db_session),
    container: ServiceContainer = Depends(get_container),
    _: object = Depends(require_admin_context),
) -> dict[str, object]:
    results = await container.memory_service.retrieve(
        session,
        user_id=payload.user_id,
        persona_id=payload.persona_id,
        query=payload.query,
        top_k=payload.top_k,
        threshold=float(container.settings.memory.similarity_threshold),
    )
    return {
        "results": [
            {
                "memory_id": str(item.memory.id),
                "score": item.score,
                "content": item.memory.content,
                "explanation": item.explanation,
            }
            for item in results
        ]
    }


@router.post("/personas")
async def upsert_persona(
    payload: PersonaUpsertRequest,
    session: AsyncSession = Depends(get_db_session),
    _: object = Depends(require_admin_context),
) -> dict[str, str]:
    stmt = select(Persona).where(Persona.key == payload.key)
    persona = (await session.execute(stmt)).scalar_one_or_none()
    if persona is None:
        persona = Persona(key=payload.key, display_name=payload.display_name)
        session.add(persona)
    data = payload.model_dump()
    elevenlabs_voice_id = str(data.pop("elevenlabs_voice_id") or "").strip()
    elevenlabs_call_model = str(data.pop("elevenlabs_call_model") or "").strip()
    elevenlabs_creative_model = str(data.pop("elevenlabs_creative_model") or "").strip()
    for key, value in data.items():
        setattr(persona, key, value)
    persona.prompt_overrides = dict(persona.prompt_overrides or {})
    if elevenlabs_voice_id:
        persona.prompt_overrides["elevenlabs_voice_id"] = elevenlabs_voice_id
    else:
        persona.prompt_overrides.pop("elevenlabs_voice_id", None)
    if elevenlabs_call_model:
        persona.prompt_overrides["elevenlabs_call_model"] = elevenlabs_call_model
    else:
        persona.prompt_overrides.pop("elevenlabs_call_model", None)
    if elevenlabs_creative_model:
        persona.prompt_overrides["elevenlabs_creative_model"] = elevenlabs_creative_model
    else:
        persona.prompt_overrides.pop("elevenlabs_creative_model", None)
    if payload.is_active:
        for other in (await session.execute(select(Persona).where(Persona.id != persona.id))).scalars().all():
            other.is_active = False
    await session.commit()
    return {"persona_id": str(persona.id)}


@router.post("/settings")
async def upsert_setting(
    payload: AppSettingUpsertRequest,
    session: AsyncSession = Depends(get_db_session),
    container: ServiceContainer = Depends(get_container),
    _: object = Depends(require_admin_context),
) -> dict[str, str]:
    setting = await container.config_service.upsert_setting(
        session,
        namespace=payload.namespace,
        key=payload.key,
        value_json=payload.value_json,
        description=payload.description,
        scope=AppSettingScope(payload.scope),
        user_id=payload.user_id,
        persona_id=payload.persona_id,
    )
    await session.commit()
    return {"setting_id": str(setting.id)}


@router.get("/state/{conversation_id}")
async def get_conversation_state(
    conversation_id: str,
    session: AsyncSession = Depends(get_db_session),
    _: object = Depends(require_admin_context),
) -> dict[str, object]:
    state = (
        await session.execute(select(ConversationState).where(ConversationState.conversation_id == conversation_id))
    ).scalar_one_or_none()
    if state is None:
        raise HTTPException(status_code=404, detail="Conversation state not found")
    return {
        "conversation_id": str(state.conversation_id),
        "active_topics": state.active_topics,
        "open_loops": state.open_loops,
        "recent_mood_trend": state.recent_mood_trend,
        "style_fingerprint": state.style_fingerprint,
        "boundary_pressure_score": state.boundary_pressure_score,
        "novelty_budget": state.novelty_budget,
        "fatigue_score": state.fatigue_score,
        "continuity_card": state.continuity_card,
        "last_archetype": state.last_archetype,
    }


@router.get("/metrics/human-likeness/{user_id}")
async def get_human_likeness_metrics(
    user_id: str,
    session: AsyncSession = Depends(get_db_session),
    _: object = Depends(require_admin_context),
) -> dict[str, object]:
    recent = list(
        (
            await session.execute(
                select(Message)
                .where(Message.user_id == user_id)
                .order_by(desc(Message.created_at))
                .limit(80)
            )
        ).scalars().all()
    )
    outbound = [item for item in recent if item.direction == Direction.outbound and item.body]
    if not outbound:
        return {"user_id": user_id, "metrics": {"repetition_rate": 0.0, "avg_length": 0.0, "safety_rewrite_rate": 0.0}}
    repeated = 0
    for idx in range(1, len(outbound)):
        if outbound[idx].normalized_body and outbound[idx - 1].normalized_body == outbound[idx].normalized_body:
            repeated += 1
    safety_rewrites = sum(
        1
        for item in recent
        if isinstance(item.metadata_json, dict)
        and isinstance(item.metadata_json.get("reply_pipeline"), dict)
        and isinstance(item.metadata_json["reply_pipeline"].get("safety_rewrite"), dict)
        and bool(item.metadata_json["reply_pipeline"]["safety_rewrite"].get("applied"))
    )
    avg_length = sum(len(item.body or "") for item in outbound) / max(len(outbound), 1)
    return {
        "user_id": user_id,
        "metrics": {
            "repetition_rate": repeated / max(len(outbound), 1),
            "avg_length": avg_length,
            "safety_rewrite_rate": safety_rewrites / max(len(outbound), 1),
        },
    }


@router.post("/evals/replay/{user_id}")
async def run_replay_evaluation(
    user_id: str,
    persona_id: str | None = None,
    max_turns: int = 20,
    session: AsyncSession = Depends(get_db_session),
    container: ServiceContainer = Depends(get_container),
    _: object = Depends(require_admin_context),
) -> dict[str, object]:
    user = await session.get(User, user_id)
    if user is None:
        raise HTTPException(status_code=404, detail="User not found")
    persona = await session.get(Persona, persona_id) if persona_id else await container.conversation_service.get_active_persona(session, user)
    config = await container.config_service.get_effective_config(session, user=user, persona=persona)
    replay = await container.human_likeness_service.run_ab_replay(
        session,
        user=user,
        persona=persona,
        config=config,
        max_turns=max_turns,
    )
    run = JobRun(
        job_name="human_likeness_replay",
        status=JobStatus.success,
        started_at=None,
        finished_at=None,
        details_json={
            "user_id": str(user.id),
            "persona_id": str(persona.id) if persona else None,
            "max_turns": max_turns,
            "summary": replay.get("summary", {}),
            "turns": replay.get("turns", 0),
            "preview": (replay.get("replay", []) or [])[:6],
        },
    )
    session.add(run)
    await session.commit()
    return {
        "user_id": str(user.id),
        "persona_id": str(persona.id) if persona else None,
        "summary": replay.get("summary", {}),
        "turns": replay.get("turns", 0),
        "sample": (replay.get("replay", []) or [])[:8],
        "job_run_id": str(run.id),
    }
