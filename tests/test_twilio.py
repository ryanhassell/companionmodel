from __future__ import annotations

import base64
import hashlib
import hmac

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
