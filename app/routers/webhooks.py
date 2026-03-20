from __future__ import annotations

import json

from fastapi import APIRouter, Depends, HTTPException, Request, WebSocket
from fastapi.responses import JSONResponse, PlainTextResponse, Response
from sqlalchemy.ext.asyncio import AsyncSession

from app.admin.dependencies import get_container
from app.db.session import get_db_session
from app.models.communication import CallRecord
from app.services.container import ServiceContainer

router = APIRouter(tags=["webhooks"])


@router.post("/webhooks/twilio/sms")
async def twilio_sms_webhook(
    request: Request,
    session: AsyncSession = Depends(get_db_session),
    container: ServiceContainer = Depends(get_container),
) -> Response:
    form = await request.form()
    signature = request.headers.get("X-Twilio-Signature")
    if not container.twilio_provider.validate_request(str(request.url), dict(form), signature):
        raise HTTPException(status_code=403, detail="Invalid Twilio signature")
    payload = container.twilio_provider.parse_inbound_form(form)
    await container.message_service.handle_inbound_message(session, payload)
    await session.commit()
    return PlainTextResponse("ok")


@router.post("/webhooks/twilio/status")
async def twilio_status_webhook(
    request: Request,
    session: AsyncSession = Depends(get_db_session),
    container: ServiceContainer = Depends(get_container),
) -> Response:
    form = await request.form()
    signature = request.headers.get("X-Twilio-Signature")
    if not container.twilio_provider.validate_request(str(request.url), dict(form), signature):
        raise HTTPException(status_code=403, detail="Invalid Twilio signature")
    await container.message_service.update_delivery_status(
        session,
        provider_sid=str(form.get("MessageSid", "")),
        message_status=str(form.get("MessageStatus", "")),
        payload=dict(form),
    )
    await session.commit()
    return PlainTextResponse("ok")


@router.api_route("/webhooks/twilio/voice", methods=["GET", "POST"])
async def twilio_voice_webhook(
    request: Request,
    session: AsyncSession = Depends(get_db_session),
    container: ServiceContainer = Depends(get_container),
) -> Response:
    if request.method == "POST":
        form = await request.form()
        signature = request.headers.get("X-Twilio-Signature")
        if not container.twilio_provider.validate_request(str(request.url), dict(form), signature):
            raise HTTPException(status_code=403, detail="Invalid Twilio signature")
        twiml = await container.voice_service.handle_twilio_voice_webhook(session, form=dict(form))
        await session.commit()
        return Response(content=twiml, media_type="application/xml")

    call_id = request.query_params.get("call_id")
    if not call_id:
        return Response(content=container.voice_service.build_hangup_twiml(), media_type="application/xml")
    record = await session.get(CallRecord, call_id)
    if record is None:
        return Response(content=container.voice_service.build_hangup_twiml(), media_type="application/xml")
    script = record.script or "Hi, just checking in and saying hello."
    twiml = container.voice_service.build_twiml(script)
    return Response(content=twiml, media_type="application/xml")


@router.post("/webhooks/twilio/voice/status")
async def twilio_voice_status_webhook(
    request: Request,
    session: AsyncSession = Depends(get_db_session),
    container: ServiceContainer = Depends(get_container),
) -> Response:
    form = await request.form()
    signature = request.headers.get("X-Twilio-Signature")
    if not container.twilio_provider.validate_request(str(request.url), dict(form), signature):
        raise HTTPException(status_code=403, detail="Invalid Twilio signature")
    await container.voice_service.update_call_status(
        session,
        provider_sid=str(form.get("CallSid", "")),
        status=str(form.get("CallStatus", "")),
    )
    await session.commit()
    return PlainTextResponse("ok")


@router.post("/webhooks/openai/realtime")
async def openai_realtime_webhook(
    request: Request,
    session: AsyncSession = Depends(get_db_session),
    container: ServiceContainer = Depends(get_container),
) -> Response:
    raw_body = await request.body()
    if not container.openai_provider.validate_realtime_webhook(
        body=raw_body,
        webhook_id=request.headers.get("webhook-id"),
        webhook_timestamp=request.headers.get("webhook-timestamp"),
        webhook_signature=request.headers.get("webhook-signature"),
    ):
        raise HTTPException(status_code=403, detail="Invalid OpenAI webhook signature")
    try:
        payload = json.loads(raw_body.decode("utf-8"))
    except json.JSONDecodeError as exc:
        raise HTTPException(status_code=400, detail="Invalid JSON payload") from exc
    result = await container.voice_service.handle_openai_realtime_event(session, payload=payload)
    await session.commit()
    return JSONResponse(result)


@router.websocket("/webhooks/twilio/voice/media-stream")
async def twilio_voice_media_stream(websocket: WebSocket) -> None:
    container = websocket.app.state.container
    await container.voice_service.handle_twilio_media_stream(websocket)
