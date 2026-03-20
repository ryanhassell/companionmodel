from __future__ import annotations

import uuid
from datetime import datetime
from typing import TYPE_CHECKING, Any

from sqlalchemy import DateTime, Enum, ForeignKey, Index, Integer, JSON, String, Text
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.db.base import Base, TimestampMixin, UUIDPrimaryKeyMixin
from app.db.types import EmbeddingVectorType
from app.models.enums import MemoryType

if TYPE_CHECKING:
    from app.models.communication import Message
    from app.models.persona import Persona
    from app.models.user import User


def enum_values(enum_cls):
    return [member.value for member in enum_cls]


class MemoryItem(UUIDPrimaryKeyMixin, TimestampMixin, Base):
    __tablename__ = "memory_items"
    __table_args__ = (
        Index("ix_memory_items_user_persona", "user_id", "persona_id"),
        Index("ix_memory_items_type", "memory_type"),
        Index("ix_memory_items_active", "disabled", "pinned"),
    )

    user_id: Mapped[uuid.UUID | None] = mapped_column(ForeignKey("users.id"))
    persona_id: Mapped[uuid.UUID | None] = mapped_column(ForeignKey("personas.id"))
    source_message_id: Mapped[uuid.UUID | None] = mapped_column(ForeignKey("messages.id"))
    consolidated_into_id: Mapped[uuid.UUID | None] = mapped_column(ForeignKey("memory_items.id"))
    memory_type: Mapped[MemoryType] = mapped_column(Enum(MemoryType, values_callable=enum_values), nullable=False)
    title: Mapped[str | None] = mapped_column(String(120))
    content: Mapped[str] = mapped_column(Text(), nullable=False)
    summary: Mapped[str | None] = mapped_column(Text())
    tags: Mapped[list[str]] = mapped_column(JSON, default=list, nullable=False)
    metadata_json: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict, nullable=False)
    importance_score: Mapped[float] = mapped_column(default=0.5, nullable=False)
    retrieval_count: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    pinned: Mapped[bool] = mapped_column(default=False, nullable=False)
    disabled: Mapped[bool] = mapped_column(default=False, nullable=False)
    embedding_model: Mapped[str | None] = mapped_column(String(120))
    embedding_text: Mapped[str | None] = mapped_column(Text())
    embedding_vector: Mapped[list[float] | None] = mapped_column(EmbeddingVectorType(1536))
    last_accessed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))

    user: Mapped[User | None] = relationship("User", back_populates="memory_items")
    persona: Mapped[Persona | None] = relationship("Persona", back_populates="memory_items")
    source_message: Mapped[Message | None] = relationship("Message")
