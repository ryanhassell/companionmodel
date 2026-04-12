from __future__ import annotations

from typing import Any, Literal

from pydantic import BaseModel, Field, field_validator, model_validator

from app.models.enums import MemoryType


class TurnClassification(BaseModel):
    intent: Literal["question", "update", "request", "emotional_share", "banter", "mixed"] = "update"
    emotion: Literal["neutral", "happy", "playful", "sad", "anxious", "distressed", "frustrated"] = "neutral"
    direct_question: bool = False
    needs_reassurance: bool = False
    risk_flags: list[str] = Field(default_factory=list)
    response_energy: Literal["low", "medium", "high"] = "medium"

    @field_validator("risk_flags", mode="before")
    @classmethod
    def _normalize_risk_flags(cls, value: Any) -> list[str]:
        if not isinstance(value, list):
            return []
        cleaned: list[str] = []
        for item in value:
            text = str(item or "").strip()
            if text and text not in cleaned:
                cleaned.append(text)
        return cleaned[:6]


class InboundActionDecision(BaseModel):
    send_image: bool = False
    reply_text: str = ""
    scene_hint: str | None = None
    include_person: bool | None = None
    reason: str = ""


class CandidateReplies(BaseModel):
    candidates: list[str] = Field(default_factory=list)

    @field_validator("candidates", mode="before")
    @classmethod
    def _normalize_candidates(cls, value: Any) -> list[str]:
        if not isinstance(value, list):
            return []
        cleaned: list[str] = []
        for item in value:
            text = " ".join(str(item or "").split()).strip()
            if text and text not in cleaned:
                cleaned.append(text)
        return cleaned[:3]


class SafetyRewriteResult(BaseModel):
    text: str = ""


class PhotoStatusReply(BaseModel):
    text: str = ""


class SupportiveSafetyReply(BaseModel):
    text: str = ""


class MemoryFactDraft(BaseModel):
    title: str = ""
    content: str = ""
    summary: str = ""
    memory_type: MemoryType = MemoryType.fact
    tags: list[str] = Field(default_factory=list)
    importance_score: float = 0.5
    supersedes_id: str | None = None
    entity_name: str | None = None
    entity_kind: str | None = None
    facet: str | None = None
    relation_to_child: str | None = None
    canonical_value: str | None = None
    related_entities: list[MemoryPlacementRelatedEntity] = Field(default_factory=list)

    @field_validator("tags", mode="before")
    @classmethod
    def _normalize_tags(cls, value: Any) -> list[str]:
        if not isinstance(value, list):
            return []
        cleaned: list[str] = []
        for item in value:
            text = str(item or "").strip()
            if text and text not in cleaned:
                cleaned.append(text)
        return cleaned[:10]

    @field_validator("importance_score", mode="before")
    @classmethod
    def _coerce_importance(cls, value: Any) -> float:
        try:
            numeric = float(value)
        except (TypeError, ValueError):
            return 0.5
        return max(0.0, min(1.0, numeric))


class MemoryExtractionResult(BaseModel):
    facts: list[MemoryFactDraft] = Field(default_factory=list)


class MemoryPlacementRelatedEntity(BaseModel):
    display_name: str = ""
    entity_kind: str = "topic"
    relation_kind: str = "related"
    relation_to_child: str | None = None
    facet: str | None = None
    canonical_value: str | None = None


class MemoryPlacementDraft(BaseModel):
    primary_name: str | None = None
    primary_kind: str = "child"
    facet: str | None = None
    relation_to_child: str | None = None
    relation_kind: str = "child_world"
    canonical_value: str | None = None
    related_entities: list[MemoryPlacementRelatedEntity] = Field(default_factory=list)


class MemorySemanticPayload(BaseModel):
    world_section: str = "memories"
    kind: str = "memory"
    group: str | None = None
    label: str | None = None
    relation: str | None = None
    path: list[str] = Field(default_factory=list)
    confidence: float = 0.65
    source_model: str | None = None
    schema_version: int = 1

    @field_validator("world_section", "kind", "group", "label", "relation", mode="before")
    @classmethod
    def _normalize_semantic_text(cls, value: Any) -> str | None:
        if value is None:
            return None
        text = " ".join(str(value).split()).strip()
        return text or None

    @field_validator("path", mode="before")
    @classmethod
    def _normalize_path(cls, value: Any) -> list[str]:
        if not isinstance(value, list):
            return []
        cleaned: list[str] = []
        for item in value:
            text = " ".join(str(item or "").split()).strip()
            if text:
                cleaned.append(text)
        return cleaned[:8]

    @field_validator("confidence", mode="before")
    @classmethod
    def _coerce_semantic_confidence(cls, value: Any) -> float:
        try:
            numeric = float(value)
        except (TypeError, ValueError):
            return 0.65
        return max(0.0, min(1.0, numeric))


class MemoryPlanEntityDraft(BaseModel):
    ref: str | None = None
    display_name: str = ""
    relation_to_child: str | None = None
    canonical_value: str | None = None
    entity_kind_legacy: str | None = None
    default_facet_legacy: str | None = None
    semantic: MemorySemanticPayload = Field(default_factory=MemorySemanticPayload)


class MemoryPlanMemoryDraft(BaseModel):
    ref: str | None = None
    memory_id: str | None = None
    title: str = ""
    content: str = ""
    summary: str | None = None
    memory_type: MemoryType = MemoryType.fact
    tags: list[str] = Field(default_factory=list)
    importance_score: float = 0.5
    entity_ref: str | None = None
    semantic: MemorySemanticPayload = Field(default_factory=MemorySemanticPayload)

    @field_validator("tags", mode="before")
    @classmethod
    def _normalize_memory_plan_tags(cls, value: Any) -> list[str]:
        if not isinstance(value, list):
            return []
        cleaned: list[str] = []
        for item in value:
            text = str(item or "").strip().lower()
            if text and text not in cleaned:
                cleaned.append(text)
        return cleaned[:12]

    @field_validator("importance_score", mode="before")
    @classmethod
    def _coerce_plan_importance(cls, value: Any) -> float:
        try:
            numeric = float(value)
        except (TypeError, ValueError):
            return 0.5
        return max(0.0, min(1.0, numeric))


class MemoryPlanAttachment(BaseModel):
    memory_ref: str | None = None
    memory_id: str | None = None
    entity_ref: str | None = None
    entity_id: str | None = None
    role: str = "primary"
    is_primary: bool = False
    facet_legacy: str | None = None
    semantic: MemorySemanticPayload = Field(default_factory=MemorySemanticPayload)


class MemoryPlanRelationDraft(BaseModel):
    parent_ref: str | None = None
    parent_id: str | None = None
    child_ref: str | None = None
    child_id: str | None = None
    relationship_kind_legacy: str | None = None
    semantic: MemorySemanticPayload = Field(default_factory=MemorySemanticPayload)


class MemoryPlanAction(BaseModel):
    action: Literal[
        "none",
        "create_memory",
        "update_memory",
        "split_memory",
        "create_entity",
        "attach_memory",
        "link_entities",
        "supersede_memory",
        "archive_memory",
    ] = "none"
    reason: str = ""
    ref: str | None = None
    target_memory_id: str | None = None
    target_memory_ref: str | None = None
    target_entity_id: str | None = None
    target_entity_ref: str | None = None
    memory: MemoryPlanMemoryDraft | None = None
    entity: MemoryPlanEntityDraft | None = None
    attachment: MemoryPlanAttachment | None = None
    relation: MemoryPlanRelationDraft | None = None
    split_parts: list[MemoryPlanMemoryDraft] = Field(default_factory=list)


class MemoryCommitPlan(BaseModel):
    summary: str = ""
    follow_up_query: str | None = None
    actions: list[MemoryPlanAction] = Field(default_factory=list)


class MemoryRecallEntity(BaseModel):
    id: str = ""
    display_name: str = ""
    entity_kind: str | None = None
    relation_to_child: str | None = None
    role: str = "primary"
    semantic: MemorySemanticPayload | None = None


class MemoryRecallRelation(BaseModel):
    source_id: str = ""
    target_id: str = ""
    relationship_type: str = ""
    semantic: MemorySemanticPayload | None = None


class MemoryNeighborhood(BaseModel):
    entities: list[MemoryRecallEntity] = Field(default_factory=list)
    relations: list[MemoryRecallRelation] = Field(default_factory=list)
    lineage: list[SavedMemoryDetail] = Field(default_factory=list)


class MemoryRecallHit(BaseModel):
    id: str = ""
    title: str = ""
    content: str = ""
    summary: str | None = None
    memory_type: str = ""
    tags: list[str] = Field(default_factory=list)
    score: float = 0.0
    explanation: str = ""
    updated_at: str | None = None
    semantic: MemorySemanticPayload | None = None
    neighborhood: MemoryNeighborhood = Field(default_factory=MemoryNeighborhood)


class MemoryRecallBundle(BaseModel):
    query: str = ""
    recent_snippets: list[str] = Field(default_factory=list)
    hits: list[MemoryRecallHit] = Field(default_factory=list)


class MemoryWriteDecision(BaseModel):
    status: Literal["applied", "skipped", "failed"] = "skipped"
    summary: str = ""
    applied_actions: list[str] = Field(default_factory=list)
    memory_ids: list[str] = Field(default_factory=list)
    entity_ids: list[str] = Field(default_factory=list)
    details: list[SavedMemoryDetail] = Field(default_factory=list)
    recall_bundle: MemoryRecallBundle | None = None
    error: str | None = None


class EntityMergeDecision(BaseModel):
    same_entity: bool = True
    title: str = ""
    content: str = ""
    summary: str = ""
    tags: list[str] = Field(default_factory=list)
    importance_score: float = 0.5


class PortalPreferencePreview(BaseModel):
    message: str = ""


class ParentGuidanceMemoryDraft(BaseModel):
    ref: str | None = None
    parent_ref: str | None = None
    title: str = ""
    content: str = ""
    memory_type: MemoryType = MemoryType.fact
    tags: list[str] = Field(default_factory=list)
    importance_score: float = 0.5
    entity_name: str | None = None
    entity_kind: str | None = None
    facet: str | None = None
    relation_to_child: str | None = None
    canonical_value: str | None = None
    related_entities: list[MemoryPlacementRelatedEntity] = Field(default_factory=list)

    @field_validator("tags", mode="before")
    @classmethod
    def _normalize_tags(cls, value: Any) -> list[str]:
        if not isinstance(value, list):
            return []
        cleaned: list[str] = []
        for item in value:
            text = str(item or "").strip().lower()
            if text and text not in cleaned:
                cleaned.append(text)
        return cleaned[:10]

    @field_validator("importance_score", mode="before")
    @classmethod
    def _coerce_importance(cls, value: Any) -> float:
        try:
            numeric = float(value)
        except (TypeError, ValueError):
            return 0.5
        return max(0.0, min(1.0, numeric))


class SavedMemoryDetail(BaseModel):
    id: str = ""
    title: str = ""
    content: str = ""
    memory_type: str = ""


class ParentGuidanceSaveResult(BaseModel):
    saved_count: int = 0
    relationship_count: int = 0
    memory_ids: list[str] = Field(default_factory=list)
    details: list[SavedMemoryDetail] = Field(default_factory=list)


class ParentChatResponse(BaseModel):
    text: str = ""


class ProactiveMessageDraft(BaseModel):
    text: str = ""


class ProactiveCallOpening(BaseModel):
    text: str = ""


class VoiceCallScript(BaseModel):
    script: str = ""


class VoiceGreeting(BaseModel):
    text: str = ""


class VoiceSummary(BaseModel):
    summary: str = ""


class SpeechDictionaryCandidate(BaseModel):
    should_confirm: bool = False
    candidate: str | None = None
    confirmation_text: str | None = None

    @model_validator(mode="after")
    def _validate_candidate(self) -> "SpeechDictionaryCandidate":
        if self.should_confirm and (not (self.candidate or "").strip() or not (self.confirmation_text or "").strip()):
            raise ValueError("candidate and confirmation_text are required when should_confirm is true")
        return self


class SpeechDictionaryConfirmation(BaseModel):
    answer: Literal["yes", "no", "unknown"] = "unknown"
