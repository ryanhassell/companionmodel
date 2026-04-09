from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any


@dataclass(slots=True)
class GeneratedImage:
    model: str
    mime_type: str
    filename_suffix: str
    binary: bytes | None = None
    remote_url: str | None = None
    revised_prompt: str | None = None
    raw_response: dict[str, Any] = field(default_factory=dict)


@dataclass(slots=True)
class SpeechResult:
    model: str
    mime_type: str
    binary: bytes


@dataclass(slots=True)
class TranscriptionResult:
    model: str
    text: str
    raw_response: dict[str, Any] = field(default_factory=dict)


@dataclass(slots=True)
class OutboundMessageResult:
    provider_sid: str | None
    status: str
    raw_response: dict[str, Any] = field(default_factory=dict)
    error_message: str | None = None


@dataclass(slots=True)
class OutboundCallResult:
    provider_sid: str | None
    status: str
    raw_response: dict[str, Any] = field(default_factory=dict)
    error_message: str | None = None


@dataclass(slots=True)
class InboundMediaPayload:
    url: str
    content_type: str | None


@dataclass(slots=True)
class InboundMessagePayload:
    from_number: str
    to_number: str | None
    body: str | None
    message_sid: str
    account_sid: str | None
    num_media: int = 0
    media: list[InboundMediaPayload] = field(default_factory=list)
    raw_form: dict[str, Any] = field(default_factory=dict)
