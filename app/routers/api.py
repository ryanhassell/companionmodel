from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.responses import FileResponse, RedirectResponse
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.admin.dependencies import get_container, require_admin_context
from app.db.session import get_db_session
from app.models.communication import MediaAsset, Message
from app.models.configuration import AppSetting
from app.models.enums import AppSettingScope
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
    for key, value in data.items():
        setattr(persona, key, value)
    persona.prompt_overrides = dict(persona.prompt_overrides or {})
    if elevenlabs_voice_id:
        persona.prompt_overrides["elevenlabs_voice_id"] = elevenlabs_voice_id
    else:
        persona.prompt_overrides.pop("elevenlabs_voice_id", None)
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
