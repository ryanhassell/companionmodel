from __future__ import annotations

import mimetypes
from typing import Any

import httpx
from tenacity import retry, retry_if_exception_type, stop_after_attempt, wait_exponential

from app.core.logging import get_logger
from app.core.security import redact_secrets
from app.core.settings import RuntimeSettings
from app.providers.base import GeneratedImage, GeneratedText, SpeechResult
from app.utils.text import extract_json_block

logger = get_logger(__name__)


class OpenAIProvider:
    def __init__(self, settings: RuntimeSettings, client: httpx.AsyncClient) -> None:
        self.settings = settings
        self.client = client

    @property
    def enabled(self) -> bool:
        return bool(self.settings.openai.api_key)

    def _headers(self) -> dict[str, str]:
        return {
            "Authorization": f"Bearer {self.settings.openai.api_key}",
            "Content-Type": "application/json",
        }

    @retry(
        stop=stop_after_attempt(3),
        wait=wait_exponential(multiplier=1, min=1, max=8),
        retry=retry_if_exception_type(httpx.HTTPError),
        reraise=True,
    )
    async def _post_json(self, endpoint: str, payload: dict[str, Any]) -> dict[str, Any]:
        url = f"{self.settings.openai.base_url.rstrip('/')}/{endpoint.lstrip('/')}"
        logger.info("openai_request", endpoint=endpoint, payload=redact_secrets(payload))
        response = await self.client.post(
            url,
            headers=self._headers(),
            json=payload,
            timeout=self.settings.openai.api_timeout_seconds,
        )
        response.raise_for_status()
        data = response.json()
        logger.info("openai_response", endpoint=endpoint, status_code=response.status_code)
        return data

    async def generate_text(
        self,
        *,
        instructions: str | None = None,
        input_items: list[dict[str, Any]] | str,
        model: str | None = None,
        temperature: float | None = None,
        max_output_tokens: int | None = None,
    ) -> GeneratedText:
        payload: dict[str, Any] = {
            "model": model or self.settings.openai.chat_model,
            "input": input_items,
            "max_output_tokens": max_output_tokens or self.settings.openai.max_output_tokens,
        }
        if instructions:
            payload["instructions"] = instructions
        if temperature is not None:
            payload["temperature"] = temperature
        if self.settings.openai.reasoning_effort:
            payload["reasoning"] = {"effort": self.settings.openai.reasoning_effort}

        data = await self._post_json("responses", payload)
        return GeneratedText(
            text=_extract_output_text(data),
            raw_response=data,
            model=data.get("model", payload["model"]),
            usage=data.get("usage", {}),
        )

    async def generate_json(
        self,
        *,
        instructions: str | None = None,
        input_items: list[dict[str, Any]] | str,
        model: str | None = None,
        max_output_tokens: int | None = None,
    ) -> dict[str, Any] | list[Any] | None:
        response = await self.generate_text(
            instructions=instructions,
            input_items=input_items,
            model=model,
            max_output_tokens=max_output_tokens,
        )
        return extract_json_block(response.text)

    async def embed_texts(self, texts: list[str], model: str | None = None) -> list[list[float]]:
        payload = {
            "model": model or self.settings.openai.embedding_model,
            "input": texts,
        }
        data = await self._post_json("embeddings", payload)
        return [item["embedding"] for item in data.get("data", [])]

    async def generate_image(
        self,
        *,
        prompt: str,
        model: str | None = None,
        size: str | None = None,
    ) -> GeneratedImage:
        payload = {
            "model": model or self.settings.openai.image_model,
            "prompt": prompt,
            "size": size or self.settings.openai.image_size,
        }
        data = await self._post_json("images/generations", payload)
        item = (data.get("data") or [{}])[0]
        mime_type = item.get("mime_type") or "image/png"
        suffix = mimetypes.guess_extension(mime_type) or ".png"
        binary = None
        if item.get("b64_json"):
            import base64

            binary = base64.b64decode(item["b64_json"])
        return GeneratedImage(
            model=payload["model"],
            mime_type=mime_type,
            filename_suffix=suffix,
            binary=binary,
            remote_url=item.get("url"),
            revised_prompt=item.get("revised_prompt"),
            raw_response=data,
        )

    async def generate_speech(
        self,
        *,
        text: str,
        voice: str,
        instructions: str | None = None,
        model: str | None = None,
        response_format: str = "mp3",
    ) -> SpeechResult:
        url = f"{self.settings.openai.base_url.rstrip('/')}/audio/speech"
        payload = {
            "model": model or self.settings.openai.speech_model,
            "voice": voice,
            "input": text,
            "response_format": response_format,
        }
        if instructions:
            payload["instructions"] = instructions
        logger.info("openai_audio_request", payload=redact_secrets(payload))
        response = await self.client.post(
            url,
            headers=self._headers(),
            json=payload,
            timeout=self.settings.openai.api_timeout_seconds,
        )
        response.raise_for_status()
        mime_type = response.headers.get("content-type", "audio/mpeg")
        return SpeechResult(model=payload["model"], mime_type=mime_type, binary=response.content)


def _extract_output_text(data: dict[str, Any]) -> str:
    if data.get("output_text"):
        return str(data["output_text"]).strip()
    output = data.get("output") or []
    texts: list[str] = []
    for item in output:
        for content in item.get("content", []):
            text = content.get("text")
            if text:
                texts.append(str(text))
    return "\n".join(texts).strip()
