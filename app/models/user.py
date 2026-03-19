from __future__ import annotations

import uuid
from datetime import datetime
from typing import TYPE_CHECKING, Any

from sqlalchemy import Boolean, DateTime, ForeignKey, Index, JSON, String, Text
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.db.base import Base, TimestampMixin, UUIDPrimaryKeyMixin

if TYPE_CHECKING:
    from app.models.communication import CallRecord, Conversation, MediaAsset, Message, SafetyEvent
    from app.models.configuration import AppSetting, ScheduleRule
    from app.models.memory import MemoryItem
    from app.models.persona import Persona


class User(UUIDPrimaryKeyMixin, TimestampMixin, Base):
    __tablename__ = "users"
    __table_args__ = (
        Index("ix_users_phone_number", "phone_number", unique=True),
        Index("ix_users_enabled", "is_enabled"),
    )

    display_name: Mapped[str | None] = mapped_column(String(120))
    phone_number: Mapped[str] = mapped_column(String(32), nullable=False)
    timezone: Mapped[str] = mapped_column(String(64), default="America/New_York", nullable=False)
    notes: Mapped[str | None] = mapped_column(Text())
    profile_json: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict, nullable=False)
    schedule_overrides: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict, nullable=False)
    safety_overrides: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict, nullable=False)
    preferred_persona_id: Mapped[uuid.UUID | None] = mapped_column(ForeignKey("personas.id"))
    is_enabled: Mapped[bool] = mapped_column(Boolean, default=True, nullable=False)
    last_inbound_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    last_outbound_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))

    preferred_persona: Mapped[Persona | None] = relationship("Persona", foreign_keys=[preferred_persona_id])
    conversations: Mapped[list[Conversation]] = relationship("Conversation", back_populates="user")
    messages: Mapped[list[Message]] = relationship("Message", back_populates="user")
    media_assets: Mapped[list[MediaAsset]] = relationship("MediaAsset", back_populates="user")
    memory_items: Mapped[list[MemoryItem]] = relationship("MemoryItem", back_populates="user")
    schedule_rules: Mapped[list[ScheduleRule]] = relationship("ScheduleRule", back_populates="user")
    settings: Mapped[list[AppSetting]] = relationship("AppSetting", back_populates="user")
    safety_events: Mapped[list[SafetyEvent]] = relationship("SafetyEvent", back_populates="user")
    call_records: Mapped[list[CallRecord]] = relationship("CallRecord", back_populates="user")
