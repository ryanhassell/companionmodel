from __future__ import annotations

from typing import Any
from uuid import UUID

from pydantic import BaseModel, Field


class SendMessageRequest(BaseModel):
    user_id: UUID
    persona_id: UUID | None = None
    body: str = Field(min_length=1, max_length=2000)
    media_asset_ids: list[UUID] = Field(default_factory=list)
    is_proactive: bool = False


class GenerateImageRequest(BaseModel):
    user_id: UUID | None = None
    persona_id: UUID
    scene_hint: str
    attach_to_message_id: UUID | None = None
    negative_prompt: str | None = None
    metadata: dict[str, Any] = Field(default_factory=dict)


class InitiateCallRequest(BaseModel):
    user_id: UUID
    persona_id: UUID | None = None
    opening_line: str | None = None


class MemorySearchRequest(BaseModel):
    user_id: UUID
    persona_id: UUID | None = None
    query: str
    top_k: int = 8


class PersonaUpsertRequest(BaseModel):
    key: str
    display_name: str
    description: str | None = None
    style: str | None = None
    tone: str | None = None
    boundaries: str | None = None
    topics_of_interest: list[str] = Field(default_factory=list)
    favorite_activities: list[str] = Field(default_factory=list)
    image_appearance: str | None = None
    speech_style: str | None = None
    disclosure_policy: str | None = None
    texting_length_preference: str | None = None
    emoji_tendency: str | None = None
    proactive_outreach_style: str | None = None
    visual_bible: dict[str, Any] = Field(default_factory=dict)
    elevenlabs_voice_id: str | None = None
    elevenlabs_call_model: str | None = None
    elevenlabs_creative_model: str | None = None
    prompt_overrides: dict[str, Any] = Field(default_factory=dict)
    safety_overrides: dict[str, Any] = Field(default_factory=dict)
    operator_notes: str | None = None
    is_active: bool = False


class AppSettingUpsertRequest(BaseModel):
    namespace: str
    key: str
    scope: str = "global"
    value_json: Any = None
    description: str | None = None
    user_id: UUID | None = None
    persona_id: UUID | None = None
