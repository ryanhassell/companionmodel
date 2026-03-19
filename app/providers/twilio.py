from __future__ import annotations

import base64
import hashlib
import hmac
from typing import Any
from urllib.parse import urlencode

import httpx
from starlette.datastructures import FormData
from tenacity import retry, retry_if_exception_type, stop_after_attempt, wait_exponential

from app.core.logging import get_logger
from app.core.security import redact_secrets
from app.core.settings import RuntimeSettings
from app.providers.base import InboundMediaPayload, InboundMessagePayload, OutboundCallResult, OutboundMessageResult

logger = get_logger(__name__)


class TwilioProvider:
    def __init__(self, settings: RuntimeSettings, client: httpx.AsyncClient) -> None:
        self.settings = settings
        self.client = client

    @property
    def enabled(self) -> bool:
        twilio = self.settings.twilio
        return bool(twilio.account_sid and twilio.auth_token and (twilio.from_number or twilio.messaging_service_sid))

    def validate_request(self, url: str, form: dict[str, Any], signature: str | None) -> bool:
        if not self.settings.twilio.validate_signatures:
            return True
        auth_token = self.settings.twilio.auth_token
        if not auth_token or not signature:
            return False
        payload = url + "".join(f"{key}{form[key]}" for key in sorted(form.keys()))
        digest = hmac.new(auth_token.encode("utf-8"), payload.encode("utf-8"), hashlib.sha1).digest()
        expected = base64.b64encode(digest).decode("utf-8")
        return hmac.compare_digest(expected, signature)

    def parse_inbound_form(self, form: FormData) -> InboundMessagePayload:
        raw = dict(form)
        num_media = int(raw.get("NumMedia", 0) or 0)
        media = []
        for index in range(num_media):
            media.append(
                InboundMediaPayload(
                    url=str(raw.get(f"MediaUrl{index}", "")),
                    content_type=raw.get(f"MediaContentType{index}"),
                )
            )
        return InboundMessagePayload(
            from_number=str(raw.get("From", "")),
            to_number=raw.get("To"),
            body=raw.get("Body"),
            message_sid=str(raw.get("MessageSid", "")),
            account_sid=raw.get("AccountSid"),
            num_media=num_media,
            media=media,
            raw_form=raw,
        )

    def _auth(self) -> httpx.BasicAuth:
        return httpx.BasicAuth(
            username=self.settings.twilio.account_sid or "",
            password=self.settings.twilio.auth_token or "",
        )

    @retry(
        stop=stop_after_attempt(3),
        wait=wait_exponential(multiplier=1, min=1, max=6),
        retry=retry_if_exception_type(httpx.HTTPError),
        reraise=True,
    )
    async def _post_form(self, path: str, data: dict[str, Any]) -> dict[str, Any]:
        account_sid = self.settings.twilio.account_sid
        if not account_sid:
            raise RuntimeError("Twilio account SID is not configured")
        url = f"https://api.twilio.com/2010-04-01/Accounts/{account_sid}/{path}"
        logger.info("twilio_request", path=path, payload=redact_secrets(data))
        response = await self.client.post(
            url,
            auth=self._auth(),
            data=data,
            timeout=self.settings.twilio.api_timeout_seconds,
        )
        response.raise_for_status()
        payload = response.json()
        logger.info("twilio_response", path=path, status_code=response.status_code)
        return payload

    async def send_message(
        self,
        *,
        to_number: str,
        body: str | None,
        media_urls: list[str] | None = None,
        status_callback: str | None = None,
        from_number: str | None = None,
    ) -> OutboundMessageResult:
        twilio = self.settings.twilio
        payload: dict[str, Any] = {
            "To": to_number,
        }
        if body:
            payload["Body"] = body
        if from_number or twilio.from_number:
            payload["From"] = from_number or twilio.from_number
        elif twilio.messaging_service_sid:
            payload["MessagingServiceSid"] = twilio.messaging_service_sid
        if status_callback or twilio.status_callback_url:
            payload["StatusCallback"] = status_callback or twilio.status_callback_url
        if media_urls:
            payload["MediaUrl"] = media_urls
        data = await self._post_form("Messages.json", payload)
        return OutboundMessageResult(
            provider_sid=data.get("sid"),
            status=data.get("status", "queued"),
            raw_response=data,
            error_message=data.get("message"),
        )

    async def initiate_call(
        self,
        *,
        to_number: str,
        twiml_url: str,
        from_number: str | None = None,
        status_callback: str | None = None,
    ) -> OutboundCallResult:
        twilio = self.settings.twilio
        payload: dict[str, Any] = {
            "To": to_number,
            "Url": twiml_url,
            "From": from_number or twilio.from_number,
        }
        if status_callback or twilio.voice_status_callback_url:
            payload["StatusCallback"] = status_callback or twilio.voice_status_callback_url
        data = await self._post_form("Calls.json", payload)
        return OutboundCallResult(
            provider_sid=data.get("sid"),
            status=data.get("status", "queued"),
            raw_response=data,
            error_message=data.get("message"),
        )

    def as_urlencoded(self, data: dict[str, Any]) -> str:
        return urlencode(data, doseq=True)
