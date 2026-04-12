from __future__ import annotations

from collections.abc import Awaitable, Callable, Sequence
from dataclasses import dataclass, field
from typing import Any

from sqlalchemy.ext.asyncio import AsyncSession

from app.models.communication import Message
from app.models.portal import ChildProfile, CustomerUser, PortalChatMessage
from app.ai.schemas import ParentGuidanceMemoryDraft, ParentGuidanceSaveResult


SaveGuidanceMemoriesFn = Callable[[list[ParentGuidanceMemoryDraft]], Awaitable[ParentGuidanceSaveResult]]


@dataclass(slots=True)
class ParentChatDeps:
    session: AsyncSession
    customer_user: CustomerUser
    child_profile: ChildProfile
    config: dict[str, Any]
    thread_messages: Sequence[PortalChatMessage]
    recent_child_messages: Sequence[Message]
    memory_hits: Sequence[Any]
    save_guidance_memories: SaveGuidanceMemoriesFn
    persona_name: str = "Resona"
    persona_description: str | None = None
    persona_style: str | None = None
    persona_tone: str | None = None
    persona_boundaries: str | None = None
    persona_speech_style: str | None = None
    persona_disclosure_policy: str | None = None
    persona_operator_notes: str | None = None
    persona_topics_of_interest: Sequence[str] = field(default_factory=tuple)
    persona_favorite_activities: Sequence[str] = field(default_factory=tuple)
    saved_memory_result: ParentGuidanceSaveResult | None = None


@dataclass(slots=True)
class VoiceContextDeps:
    recent_call_context: str = ""
    speech_dictionary_entries: list[dict[str, str]] = field(default_factory=list)
