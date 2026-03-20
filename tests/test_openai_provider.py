from __future__ import annotations

import base64
import hashlib
import hmac

import httpx
import respx

from app.providers.openai import OpenAIProvider


@respx.mock
async def test_openai_generate_text_parses_output_text(settings):
    settings.openai.api_key = "key"
    client = httpx.AsyncClient()
    provider = OpenAIProvider(settings, client)
    route = respx.post("https://api.openai.com/v1/responses").mock(
        return_value=httpx.Response(
            200,
            json={"model": "gpt-test", "output_text": "hello world", "usage": {"total_tokens": 3}},
        )
    )
    response = await provider.generate_text(input_items="hello")
    assert route.called
    assert response.text == "hello world"
    await client.aclose()


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
