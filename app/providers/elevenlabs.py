from __future__ import annotations

from collections.abc import AsyncIterator
from typing import Any

import httpx

from app.core.logging import get_logger
from app.core.settings import RuntimeSettings

logger = get_logger(__name__)


class ElevenLabsProvider:
    def __init__(self, settings: RuntimeSettings, client: httpx.AsyncClient) -> None:
        self.settings = settings
        self.client = client

    @property
    def enabled(self) -> bool:
        return bool(self.settings.elevenlabs.api_key)

    def _headers(self) -> dict[str, str]:
        return {
            "xi-api-key": self.settings.elevenlabs.api_key or "",
            "Accept": "application/octet-stream",
            "Content-Type": "application/json",
        }

    async def stream_tts(
        self,
        *,
        text: str,
        voice_id: str,
        model_id: str | None = None,
        output_format: str = "ulaw_8000",
    ) -> AsyncIterator[bytes]:
        url = f"{self.settings.elevenlabs.base_url.rstrip('/')}/text-to-speech/{voice_id}/stream"
        payload = {
            "text": text,
            "model_id": model_id or self.settings.voice.elevenlabs_tts_model or self.settings.elevenlabs.tts_model,
            "output_format": output_format,
        }
        logger.info(
            "elevenlabs_tts_request",
            voice_id=voice_id,
            model_id=payload["model_id"],
            output_format=output_format,
            text_preview=text[:160],
        )
        async with self.client.stream(
            "POST",
            url,
            headers=self._headers(),
            json=payload,
            timeout=self.settings.elevenlabs.api_timeout_seconds,
        ) as response:
            response.raise_for_status()
            async for chunk in response.aiter_bytes():
                if chunk:
                    yield chunk
        logger.info(
            "elevenlabs_tts_response",
            voice_id=voice_id,
            model_id=payload["model_id"],
            output_format=output_format,
        )
