from __future__ import annotations

import base64
import hashlib
import hmac

import httpx
import respx
from starlette.datastructures import FormData

from app.providers.twilio import TwilioProvider


def _signature(url: str, form: dict[str, str], auth_token: str) -> str:
    payload = url + "".join(f"{key}{form[key]}" for key in sorted(form.keys()))
    digest = hmac.new(auth_token.encode("utf-8"), payload.encode("utf-8"), hashlib.sha1).digest()
    return base64.b64encode(digest).decode("utf-8")


def test_twilio_signature_validation(settings):
    settings.twilio.auth_token = "secret"
    provider = TwilioProvider(settings, client=None)
    url = "https://example.com/webhooks/twilio/sms"
    form = {"Body": "hello", "From": "+1555"}
    signature = _signature(url, form, "secret")
    assert provider.validate_request(url, form, signature) is True


def test_twilio_inbound_parsing(settings):
    provider = TwilioProvider(settings, client=None)
    form = FormData(
        {
            "From": "+1555",
            "To": "+1666",
            "Body": "hello",
            "MessageSid": "SM123",
            "NumMedia": "1",
            "MediaUrl0": "https://example.com/x.jpg",
            "MediaContentType0": "image/jpeg",
        }
    )
    payload = provider.parse_inbound_form(form)
    assert payload.message_sid == "SM123"
    assert payload.num_media == 1
    assert payload.media[0].content_type == "image/jpeg"


@respx.mock
async def test_twilio_initiate_call_supports_inline_twiml(settings):
    settings.twilio.account_sid = "AC123"
    settings.twilio.auth_token = "secret"
    settings.twilio.from_number = "+15550001111"
    client = httpx.AsyncClient()
    provider = TwilioProvider(settings, client)
    route = respx.post("https://api.twilio.com/2010-04-01/Accounts/AC123/Calls.json").mock(
        return_value=httpx.Response(200, json={"sid": "CA123", "status": "queued"})
    )
    result = await provider.initiate_call(
        to_number="+15550002222",
        twiml="<Response><Dial><Sip>sip:test</Sip></Dial></Response>",
    )
    assert route.called
    assert result.provider_sid == "CA123"
    assert "Twiml=%3CResponse%3E" in route.calls[0].request.content.decode("utf-8")
    await client.aclose()
