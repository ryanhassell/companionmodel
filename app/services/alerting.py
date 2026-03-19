from __future__ import annotations

from typing import Any

import httpx

from app.core.logging import get_logger
from app.core.settings import RuntimeSettings

logger = get_logger(__name__)


class AlertingService:
    def __init__(self, settings: RuntimeSettings, client: httpx.AsyncClient) -> None:
        self.settings = settings
        self.client = client

    async def send_alert(self, payload: dict[str, Any]) -> None:
        webhook_url = self.settings.alerting.webhook_url
        if not webhook_url:
            logger.info("alert_skipped", reason="webhook_not_configured", payload=payload)
            return
        response = await self.client.post(webhook_url, json=payload, timeout=10)
        response.raise_for_status()
        logger.info("alert_sent", status_code=response.status_code)
