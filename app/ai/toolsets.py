from __future__ import annotations

from typing import Any

from pydantic import BaseModel, Field


class VoiceRecentContextArgs(BaseModel):
    query: str = Field(default="", description="What kind of recent context the model wants surfaced.")


class VoiceSaveCallMemoryArgs(BaseModel):
    title: str = Field(description="Short memory title.")
    content: str = Field(description="Memory content to save from the call.")
    summary: str | None = Field(default=None, description="Optional short summary for the memory.")
    memory_type: str = Field(default="fact", description="Memory type such as fact, preference, summary, or safety.")
    importance_score: float = Field(default=0.7, description="Importance score between 0 and 1.")
    tags: list[str] = Field(default_factory=list, description="Short tags for retrieval.")
    entity_name: str | None = Field(default=None, description="Optional entity name if this memory is about a recurring person, pet, or thing.")
    entity_kind: str | None = Field(default=None, description="Optional entity kind such as person, pet, artist, or place.")


class VoiceEndCallArgs(BaseModel):
    reason: str = Field(default="conversation_complete", description="Why the call should end.")


class VoiceToolDefinition(BaseModel):
    type: str = "function"
    name: str
    description: str
    parameters: dict[str, Any]


def voice_realtime_tool_definitions() -> list[dict[str, Any]]:
    return [
        VoiceToolDefinition(
            name="get_recent_context",
            description="Look up short recent conversational context before responding.",
            parameters=VoiceRecentContextArgs.model_json_schema(),
        ).model_dump(),
        VoiceToolDefinition(
            name="save_call_memory",
            description="Save an important memory from the live call for future context.",
            parameters=VoiceSaveCallMemoryArgs.model_json_schema(),
        ).model_dump(),
        VoiceToolDefinition(
            name="end_call",
            description="End the call when the conversation has reached a natural stopping point or needs to stop for safety.",
            parameters=VoiceEndCallArgs.model_json_schema(),
        ).model_dump(),
    ]
