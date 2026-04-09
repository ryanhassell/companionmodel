from __future__ import annotations

import uuid
from typing import Any

from sqlalchemy import Float, ForeignKey, Index, JSON, String, Text
from sqlalchemy.orm import Mapped, mapped_column

from app.db.base import Base, TimestampMixin, UUIDPrimaryKeyMixin


class ConversationState(UUIDPrimaryKeyMixin, TimestampMixin, Base):
    __tablename__ = "conversation_states"
    __table_args__ = (
        Index("ix_conversation_states_conversation", "conversation_id", unique=True),
        Index("ix_conversation_states_user_persona", "user_id", "persona_id"),
    )

    conversation_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("conversations.id"), nullable=False)
    user_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("users.id"), nullable=False)
    persona_id: Mapped[uuid.UUID | None] = mapped_column(ForeignKey("personas.id"))

    active_topics: Mapped[list[str]] = mapped_column(JSON, default=list, nullable=False)
    open_loops: Mapped[list[dict[str, Any]]] = mapped_column(JSON, default=list, nullable=False)
    last_user_questions: Mapped[list[str]] = mapped_column(JSON, default=list, nullable=False)
    recent_mood_trend: Mapped[str | None] = mapped_column(String(40))
    style_fingerprint: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict, nullable=False)
    boundary_pressure_score: Mapped[float] = mapped_column(Float, default=0.0, nullable=False)
    novelty_budget: Mapped[float] = mapped_column(Float, default=1.0, nullable=False)
    fatigue_score: Mapped[float] = mapped_column(Float, default=0.0, nullable=False)
    last_archetype: Mapped[str | None] = mapped_column(String(40))
    continuity_card: Mapped[str | None] = mapped_column(Text())
    relationship_milestones: Mapped[list[str]] = mapped_column(JSON, default=list, nullable=False)
    metadata_json: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict, nullable=False)
