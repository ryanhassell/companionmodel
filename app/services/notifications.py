from __future__ import annotations

import asyncio
import smtplib
from email.message import EmailMessage

from app.core.logging import get_logger
from app.core.settings import RuntimeSettings
from app.providers.twilio import TwilioProvider

logger = get_logger(__name__)


class NotificationService:
    def __init__(self, settings: RuntimeSettings, twilio_provider: TwilioProvider) -> None:
        self.settings = settings
        self.twilio_provider = twilio_provider

    async def send_verification_email(
        self,
        *,
        to_email: str,
        display_name: str | None,
        verify_token: str,
    ) -> bool:
        if not (self.settings.email.enabled and self.settings.email.smtp_host and self.settings.email.from_address):
            logger.warning("email_not_configured", to_email=to_email)
            return False

        subject = "Verify your Companion Parent Portal account"
        greeting = display_name or "there"
        verify_link = f"{self.settings.app.base_url}/app/verify"
        body = (
            f"Hi {greeting},\n\n"
            "Your verification token is:\n"
            f"{verify_token}\n\n"
            f"Enter it at: {verify_link}\n\n"
            "If you did not request this, you can ignore this message."
        )

        message = EmailMessage()
        message["Subject"] = subject
        message["From"] = self.settings.email.from_address
        message["To"] = to_email
        message.set_content(body)

        try:
            await asyncio.to_thread(self._send_email_blocking, message)
            logger.info("verification_email_sent", to_email=to_email)
            return True
        except Exception as exc:  # noqa: BLE001
            logger.exception("verification_email_failed", to_email=to_email, error=str(exc))
            return False

    def _send_email_blocking(self, message: EmailMessage) -> None:
        email_cfg = self.settings.email
        if email_cfg.use_ssl:
            client = smtplib.SMTP_SSL(email_cfg.smtp_host, email_cfg.smtp_port, timeout=15)
        else:
            client = smtplib.SMTP(email_cfg.smtp_host, email_cfg.smtp_port, timeout=15)
        with client:
            client.ehlo()
            if email_cfg.use_starttls and not email_cfg.use_ssl:
                client.starttls()
                client.ehlo()
            if email_cfg.smtp_username:
                client.login(email_cfg.smtp_username, email_cfg.smtp_password or "")
            client.send_message(message)

    async def send_verification_sms(self, *, to_number: str, otp_code: str) -> bool:
        if not self.twilio_provider.enabled:
            logger.warning("sms_not_configured", to_number=to_number)
            return False
        body = f"Your Companion verification code is {otp_code}. It expires in {self.settings.customer_portal.otp_code_minutes} minutes."
        try:
            result = await self.twilio_provider.send_message(to_number=to_number, body=body)
            ok = result.status not in {"failed", "undelivered"}
            if ok:
                logger.info("verification_sms_sent", to_number=to_number, sid=result.provider_sid)
            else:
                logger.warning("verification_sms_failed", to_number=to_number, sid=result.provider_sid, status=result.status)
            return ok
        except Exception as exc:  # noqa: BLE001
            logger.exception("verification_sms_failed", to_number=to_number, error=str(exc))
            return False
