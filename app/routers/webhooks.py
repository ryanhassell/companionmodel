from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.responses import PlainTextResponse, Response
from sqlalchemy.ext.asyncio import AsyncSession

from app.admin.dependencies import get_container
from app.db.session import get_db_session
from app.models.communication import CallRecord
from app.services.container import ServiceContainer

router = APIRouter(prefix="/webhooks/twilio", tags=["webhooks"])


@router.post("/sms")
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


@router.post("/status")
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


@router.api_route("/voice", methods=["GET", "POST"])
async def twilio_voice_webhook(
    request: Request,
    call_id: str,
    session: AsyncSession = Depends(get_db_session),
) -> Response:
    record = await session.get(CallRecord, call_id)
    if record is None:
        return Response(
            content='<?xml version="1.0" encoding="UTF-8"?><Response><Hangup/></Response>',
            media_type="application/xml",
        )
    script = record.script or "Hi, just checking in and saying hello."
    container = request.app.state.container
    twiml = container.voice_service.build_twiml(script)
    return Response(content=twiml, media_type="application/xml")


@router.post("/voice/status")
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
