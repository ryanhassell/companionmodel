from __future__ import annotations

import base64
import hashlib
import hmac
import mimetypes
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any

import httpx
from tenacity import retry, retry_if_exception_type, stop_after_attempt, wait_exponential

from app.core.logging import get_logger
from app.core.security import redact_secrets
from app.core.settings import RuntimeSettings
from app.providers.base import GeneratedImage, GeneratedText, SpeechResult, TranscriptionResult
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

    def _websocket_url(self, call_id: str) -> str:
        base = self.settings.openai.base_url.rstrip("/")
        if base.startswith("https://"):
            base = "wss://" + base[len("https://") :]
        elif base.startswith("http://"):
            base = "ws://" + base[len("http://") :]
        return f"{base}/realtime?call_id={call_id}"

    @retry(
        stop=stop_after_attempt(3),
        wait=wait_exponential(multiplier=1, min=1, max=8),
        retry=retry_if_exception_type(httpx.HTTPError),
        reraise=True,
    )
    async def _post_json(self, endpoint: str, payload: dict[str, Any]) -> dict[str, Any]:
        return await self._post_json_once(endpoint, payload)

    async def _post_json_once(self, endpoint: str, payload: dict[str, Any]) -> dict[str, Any]:
        url = f"{self.settings.openai.base_url.rstrip('/')}/{endpoint.lstrip('/')}"
        if endpoint == "images/generations":
            logger.info(
                "openai_image_generation_request",
                endpoint=endpoint,
                model=payload.get("model"),
                size=payload.get("size"),
                prompt_preview=_preview_text(payload.get("prompt")),
            )
        elif endpoint == "responses":
            logger.info(
                "openai_text_request",
                endpoint=endpoint,
                model=payload.get("model"),
                max_output_tokens=payload.get("max_output_tokens"),
                input_preview=_preview_input(payload.get("input")),
                instructions_preview=_preview_text(payload.get("instructions")),
            )
        elif endpoint == "embeddings":
            logger.info(
                "openai_embeddings_request",
                endpoint=endpoint,
                model=payload.get("model"),
                input_count=len(payload.get("input") or []),
                input_preview=_preview_embedding_inputs(payload.get("input")),
            )
        else:
            logger.info("openai_request", endpoint=endpoint, payload=redact_secrets(payload))
        response = await self.client.post(
            url,
            headers=self._headers(),
            json=payload,
            timeout=self.settings.openai.api_timeout_seconds,
        )
        if response.is_error:
            if endpoint == "images/generations":
                logger.info(
                    "openai_image_generation_error",
                    endpoint=endpoint,
                    status_code=response.status_code,
                    body_preview=_preview_text(response.text, limit=400),
                )
            elif endpoint == "responses":
                logger.info(
                    "openai_text_error",
                    endpoint=endpoint,
                    status_code=response.status_code,
                    body_preview=_preview_text(response.text, limit=400),
                )
            elif endpoint == "embeddings":
                logger.info(
                    "openai_embeddings_error",
                    endpoint=endpoint,
                    status_code=response.status_code,
                    body_preview=_preview_text(response.text, limit=400),
                )
            else:
                logger.info(
                    "openai_error_response",
                    endpoint=endpoint,
                    status_code=response.status_code,
                    body=response.text,
                )
        response.raise_for_status()
        if not response.content or not response.content.strip():
            data = {}
        else:
            data = response.json()
        if endpoint == "images/generations":
            item = (data.get("data") or [{}])[0]
            logger.info(
                "openai_image_generation_response",
                endpoint=endpoint,
                status_code=response.status_code,
                mime_type=item.get("mime_type"),
                has_b64=bool(item.get("b64_json")),
                has_url=bool(item.get("url")),
                revised_prompt_preview=_preview_text(item.get("revised_prompt")),
            )
        elif endpoint == "responses":
            logger.info(
                "openai_text_response",
                endpoint=endpoint,
                status_code=response.status_code,
                output_preview=_preview_text(_extract_output_text(data)),
            )
        elif endpoint == "embeddings":
            logger.info(
                "openai_embeddings_response",
                endpoint=endpoint,
                status_code=response.status_code,
                output_count=len(data.get("data") or []),
            )
        else:
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
        chosen_model = model or self.settings.openai.chat_model
        payload: dict[str, Any] = {
            "model": chosen_model,
            "input": input_items,
            "max_output_tokens": max_output_tokens or self.settings.openai.max_output_tokens,
        }
        if instructions:
            payload["instructions"] = instructions
        if temperature is not None and _supports_temperature(chosen_model, self.settings.openai.reasoning_effort):
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

    async def accept_realtime_call(self, call_id: str, *, payload: dict[str, Any]) -> dict[str, Any]:
        return await self._post_json_once(
            f"realtime/calls/{call_id}/accept",
            payload,
        )

    async def end_realtime_call(self, call_id: str) -> dict[str, Any]:
        return await self._post_json_once(f"realtime/calls/{call_id}/end", {})

    def validate_realtime_webhook(
        self,
        *,
        body: bytes,
        webhook_id: str | None,
        webhook_timestamp: str | None,
        webhook_signature: str | None,
    ) -> bool:
        if not self.settings.openai.validate_realtime_webhooks:
            logger.info("openai_realtime_webhook_validation_bypassed")
            return True
        secret = self.settings.openai.realtime_webhook_secret
        if not secret:
            return True
        if not webhook_id or not webhook_timestamp or not webhook_signature:
            return False
        signed = f"{webhook_id}.{webhook_timestamp}.{body.decode('utf-8')}".encode("utf-8")
        digest = hmac.new(secret.encode("utf-8"), signed, hashlib.sha256).digest()
        expected = base64.b64encode(digest).decode("utf-8")
        for part in [item.strip() for item in webhook_signature.split() if item.strip()]:
            candidate = part
            if "," in candidate:
                version, value = candidate.split(",", 1)
                if version.strip() != "v1":
                    continue
                candidate = value.strip()
            if "=" in candidate:
                version, value = candidate.split("=", 1)
                if version.strip() != "v1":
                    continue
                candidate = value.strip()
            if hmac.compare_digest(candidate, expected):
                return True
        return False

    @asynccontextmanager
    async def open_realtime_sideband(self, call_id: str):
        try:
            from websockets.asyncio.client import connect
        except Exception as exc:  # pragma: no cover
            try:
                from websockets.client import connect  # type: ignore
            except Exception:
                raise RuntimeError("websockets package is required for realtime sideband connections") from exc
        async with connect(
            self._websocket_url(call_id),
            additional_headers=[("Authorization", f"Bearer {self.settings.openai.api_key}")],
            open_timeout=self.settings.voice.sideband_connect_timeout_seconds,
            close_timeout=10,
        ) as websocket:
            yield websocket

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

    async def transcribe_audio(
        self,
        *,
        audio_bytes: bytes,
        filename: str = "audio.wav",
        mime_type: str = "audio/wav",
        model: str | None = None,
        prompt: str | None = None,
        language: str | None = None,
    ) -> TranscriptionResult:
        chosen_model = model or self.settings.voice.stt_model
        url = f"{self.settings.openai.base_url.rstrip('/')}/audio/transcriptions"
        data: dict[str, Any] = {"model": chosen_model}
        if prompt:
            data["prompt"] = prompt
        if language:
            data["language"] = language
        logger.info(
            "openai_transcription_request",
            endpoint="audio/transcriptions",
            model=chosen_model,
            filename=filename,
            mime_type=mime_type,
            size_bytes=len(audio_bytes),
            prompt_preview=_preview_text(prompt),
        )
        response = await self.client.post(
            url,
            headers={"Authorization": f"Bearer {self.settings.openai.api_key}"},
            data=data,
            files={"file": (filename, audio_bytes, mime_type)},
            timeout=self.settings.openai.api_timeout_seconds,
        )
        if response.is_error:
            logger.info(
                "openai_transcription_error",
                endpoint="audio/transcriptions",
                status_code=response.status_code,
                body_preview=_preview_text(response.text, limit=400),
            )
        response.raise_for_status()
        payload = response.json()
        logger.info(
            "openai_transcription_response",
            endpoint="audio/transcriptions",
            status_code=response.status_code,
            text_preview=_preview_text(payload.get("text")),
        )
        return TranscriptionResult(
            model=chosen_model,
            text=str(payload.get("text") or "").strip(),
            raw_response=payload,
        )

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
        data = await self._post_json_once("images/generations", payload)
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

    async def edit_image(
        self,
        *,
        prompt: str,
        reference_images: list[Path],
        model: str | None = None,
        size: str | None = None,
    ) -> GeneratedImage:
        url = f"{self.settings.openai.base_url.rstrip('/')}/images/edits"
        chosen_model = model or self.settings.openai.image_model
        files: list[tuple[str, tuple[str, bytes, str]]] = []
        for path in reference_images:
            mime_type = mimetypes.guess_type(path.name)[0] or "image/png"
            files.append(("image[]", (path.name, path.read_bytes(), mime_type)))
        data: dict[str, Any] = {
            "model": chosen_model,
            "prompt": prompt,
            "size": size or self.settings.openai.image_size,
        }
        logger.info(
            "openai_image_edit_request",
            model=chosen_model,
            size=data["size"],
            reference_image_count=len(reference_images),
            reference_images=[str(path) for path in reference_images],
            prompt_preview=_preview_text(prompt),
        )
        response = await self.client.post(
            url,
            headers={"Authorization": f"Bearer {self.settings.openai.api_key}"},
            data=data,
            files=files,
            timeout=self.settings.openai.image_api_timeout_seconds,
        )
        if response.is_error:
            logger.info(
                "openai_image_edit_error",
                endpoint="images/edits",
                status_code=response.status_code,
                body_preview=_preview_text(response.text, limit=400),
            )
        response.raise_for_status()
        data = response.json()
        item = (data.get("data") or [{}])[0]
        logger.info(
            "openai_image_edit_response",
            endpoint="images/edits",
            status_code=response.status_code,
            mime_type=item.get("mime_type"),
            has_b64=bool(item.get("b64_json")),
            has_url=bool(item.get("url")),
            revised_prompt_preview=_preview_text(item.get("revised_prompt")),
        )
        mime_type = item.get("mime_type") or "image/png"
        suffix = mimetypes.guess_extension(mime_type) or ".png"
        binary = None
        if item.get("b64_json"):
            import base64

            binary = base64.b64decode(item["b64_json"])
        return GeneratedImage(
            model=chosen_model,
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
            timeout=self.settings.openai.image_api_timeout_seconds,
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


def _supports_temperature(model: str, reasoning_effort: str | None) -> bool:
    normalized = model.strip().lower()
    if normalized.startswith("gpt-5") and reasoning_effort not in (None, "", "none"):
        return False
    return True


def _preview_text(value: Any, limit: int = 220) -> str | None:
    if value is None:
        return None
    text = " ".join(str(value).split())
    if len(text) <= limit:
        return text
    return f"{text[:limit-3]}..."


def _preview_input(value: Any) -> str | None:
    if isinstance(value, str):
        return _preview_text(value)
    if not isinstance(value, list):
        return _preview_text(value)
    parts: list[str] = []
    for item in value[:2]:
        if not isinstance(item, dict):
            continue
        content = item.get("content")
        if isinstance(content, str):
            parts.append(content)
        elif isinstance(content, list):
            for block in content:
                if isinstance(block, dict) and isinstance(block.get("text"), str):
                    parts.append(block["text"])
                    break
    return _preview_text(" | ".join(parts))


def _preview_embedding_inputs(value: Any) -> str | None:
    if not isinstance(value, list):
        return _preview_text(value)
    return " | ".join(
        preview for preview in (_preview_text(item, limit=100) for item in value[:2]) if preview
    )
