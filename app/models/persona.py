from __future__ import annotations

import uuid
from typing import TYPE_CHECKING, Any

from sqlalchemy import Boolean, ForeignKey, Index, JSON, String, Text
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.db.base import Base, TimestampMixin, UUIDPrimaryKeyMixin

if TYPE_CHECKING:
    from app.models.communication import CallRecord, Conversation, MediaAsset, Message, SafetyEvent
    from app.models.configuration import AppSetting, ScheduleRule
    from app.models.memory import MemoryItem
    from app.models.portal import Account
    from app.models.user import User


class Persona(UUIDPrimaryKeyMixin, TimestampMixin, Base):
    __tablename__ = "personas"
    __table_args__ = (
        Index("ix_personas_key", "key", unique=True),
        Index("ix_personas_active", "is_active"),
        Index("ix_personas_source", "source_type", "updated_at"),
        Index("ix_personas_account_owner", "account_id", "owner_user_id"),
    )

    key: Mapped[str] = mapped_column(String(80), nullable=False)
    account_id: Mapped[uuid.UUID | None] = mapped_column(ForeignKey("accounts.id"))
    owner_user_id: Mapped[uuid.UUID | None] = mapped_column(ForeignKey("users.id"))
    source_type: Mapped[str] = mapped_column(String(40), nullable=False, default="admin")
    preset_key: Mapped[str | None] = mapped_column(String(80))
    display_name: Mapped[str] = mapped_column(String(120), nullable=False)
    description: Mapped[str | None] = mapped_column(Text())
    style: Mapped[str | None] = mapped_column(Text())
    tone: Mapped[str | None] = mapped_column(Text())
    boundaries: Mapped[str | None] = mapped_column(Text())
    topics_of_interest: Mapped[list[str]] = mapped_column(JSON, default=list, nullable=False)
    favorite_activities: Mapped[list[str]] = mapped_column(JSON, default=list, nullable=False)
    image_appearance: Mapped[str | None] = mapped_column(Text())
    speech_style: Mapped[str | None] = mapped_column(Text())
    disclosure_policy: Mapped[str | None] = mapped_column(Text())
    texting_length_preference: Mapped[str | None] = mapped_column(String(32))
    emoji_tendency: Mapped[str | None] = mapped_column(String(32))
    proactive_outreach_style: Mapped[str | None] = mapped_column(Text())
    visual_bible: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict, nullable=False)
    prompt_overrides: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict, nullable=False)
    safety_overrides: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict, nullable=False)
    operator_notes: Mapped[str | None] = mapped_column(Text())
    is_active: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)

    account: Mapped[Account | None] = relationship("Account")
    owner_user: Mapped[User | None] = relationship("User", foreign_keys=[owner_user_id])
    users: Mapped[list[User]] = relationship("User", back_populates="preferred_persona", foreign_keys="User.preferred_persona_id")
    conversations: Mapped[list[Conversation]] = relationship("Conversation", back_populates="persona")
    messages: Mapped[list[Message]] = relationship("Message", back_populates="persona")
    media_assets: Mapped[list[MediaAsset]] = relationship("MediaAsset", back_populates="persona")
    memory_items: Mapped[list[MemoryItem]] = relationship("MemoryItem", back_populates="persona")
    schedule_rules: Mapped[list[ScheduleRule]] = relationship("ScheduleRule", back_populates="persona")
    settings: Mapped[list[AppSetting]] = relationship("AppSetting", back_populates="persona")
    safety_events: Mapped[list[SafetyEvent]] = relationship("SafetyEvent", back_populates="persona")
    call_records: Mapped[list[CallRecord]] = relationship("CallRecord", back_populates="persona")
