from __future__ import annotations

import base64
import hashlib
import hmac

import httpx
import respx

from app.providers.openai import OpenAIProvider


@respx.mock
async def test_openai_accept_realtime_call_hits_expected_endpoint(settings):
    settings.openai.api_key = "key"
    client = httpx.AsyncClient()
    provider = OpenAIProvider(settings, client)
    route = respx.post("https://api.openai.com/v1/realtime/calls/call_123/accept").mock(
        return_value=httpx.Response(200, json={"id": "call_123", "status": "accepted"})
    )
    payload = await provider.accept_realtime_call(
        "call_123",
        payload={"type": "realtime", "model": "gpt-realtime-mini"},
    )
    assert route.called
    assert payload["status"] == "accepted"
    await client.aclose()


@respx.mock
async def test_openai_transcribe_audio_hits_expected_endpoint(settings):
    settings.openai.api_key = "key"
    client = httpx.AsyncClient()
    provider = OpenAIProvider(settings, client)
    route = respx.post("https://api.openai.com/v1/audio/transcriptions").mock(
        return_value=httpx.Response(200, json={"text": "hello there"})
    )
    result = await provider.transcribe_audio(audio_bytes=b"RIFF....", filename="call.wav")
    assert route.called
    assert result.text == "hello there"
    await client.aclose()


@respx.mock
async def test_openai_generate_image_hits_expected_endpoint(settings):
    settings.openai.api_key = "key"
    client = httpx.AsyncClient()
    provider = OpenAIProvider(settings, client)
    route = respx.post("https://api.openai.com/v1/images/generations").mock(
        return_value=httpx.Response(
            200,
            json={
                "data": [
                    {
                        "mime_type": "image/png",
                        "b64_json": base64.b64encode(b"png-bytes").decode("utf-8"),
                        "revised_prompt": "sunrise over a lake",
                    }
                ]
            },
        )
    )
    result = await provider.generate_image(prompt="sunrise over a lake")
    assert route.called
    assert result.mime_type == "image/png"
    assert result.binary == b"png-bytes"
    await client.aclose()


def test_openai_realtime_webhook_validation(settings):
    settings.openai.realtime_webhook_secret = "whsec_test"
    provider = OpenAIProvider(settings, httpx.AsyncClient())
    body = b'{"type":"realtime.call.incoming","call_id":"call_123"}'
    webhook_id = "wh_123"
    webhook_timestamp = "1710000000"
    signed = f"{webhook_id}.{webhook_timestamp}.{body.decode('utf-8')}".encode("utf-8")
    digest = hmac.new(settings.openai.realtime_webhook_secret.encode("utf-8"), signed, hashlib.sha256).digest()
    signature = "v1=" + base64.b64encode(digest).decode("utf-8")
    assert provider.validate_realtime_webhook(
        body=body,
        webhook_id=webhook_id,
        webhook_timestamp=webhook_timestamp,
        webhook_signature=signature,
    )
