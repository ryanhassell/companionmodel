from __future__ import annotations

import uuid
from datetime import datetime
from typing import TYPE_CHECKING, Any

from sqlalchemy import DateTime, Enum, ForeignKey, Index, Integer, JSON, String, Text
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.db.base import Base, TimestampMixin, UUIDPrimaryKeyMixin
from app.models.enums import CallDirection, CallStatus, Channel, DeliveryStatus, Direction, MediaRole, MessageStatus, SafetySeverity

if TYPE_CHECKING:
    from app.models.persona import Persona
    from app.models.user import User


def enum_values(enum_cls):
    return [member.value for member in enum_cls]


class Conversation(UUIDPrimaryKeyMixin, TimestampMixin, Base):
    __tablename__ = "conversations"
    __table_args__ = (
        Index("ix_conversations_user_persona", "user_id", "persona_id"),
        Index("ix_conversations_status", "status"),
    )

    user_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("users.id"), nullable=False)
    persona_id: Mapped[uuid.UUID | None] = mapped_column(ForeignKey("personas.id"))
    status: Mapped[str] = mapped_column(String(32), default="open", nullable=False)
    state_label: Mapped[str | None] = mapped_column(String(64))
    unresolved_thread_summary: Mapped[str | None] = mapped_column(Text())
    metadata_json: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict, nullable=False)
    last_inbound_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    last_outbound_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))

    user: Mapped[User] = relationship("User", back_populates="conversations")
    persona: Mapped[Persona | None] = relationship("Persona", back_populates="conversations")
    messages: Mapped[list[Message]] = relationship("Message", back_populates="conversation")


class Message(UUIDPrimaryKeyMixin, TimestampMixin, Base):
    __tablename__ = "messages"
    __table_args__ = (
        Index("ix_messages_conversation_created", "conversation_id", "created_at"),
        Index("ix_messages_provider_sid", "provider_message_sid", unique=True),
        Index("ix_messages_direction_status", "direction", "status"),
        Index("ix_messages_idempotency_key", "idempotency_key", unique=True),
    )

    conversation_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("conversations.id"), nullable=False)
    user_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("users.id"), nullable=False)
    persona_id: Mapped[uuid.UUID | None] = mapped_column(ForeignKey("personas.id"))
    direction: Mapped[Direction] = mapped_column(Enum(Direction, values_callable=enum_values), nullable=False)
    channel: Mapped[Channel] = mapped_column(Enum(Channel, values_callable=enum_values), nullable=False)
    provider: Mapped[str] = mapped_column(String(40), default="twilio", nullable=False)
    provider_message_sid: Mapped[str | None] = mapped_column(String(80))
    idempotency_key: Mapped[str] = mapped_column(String(120), nullable=False)
    body: Mapped[str | None] = mapped_column(Text())
    normalized_body: Mapped[str | None] = mapped_column(Text())
    status: Mapped[MessageStatus] = mapped_column(Enum(MessageStatus, values_callable=enum_values), nullable=False)
    is_proactive: Mapped[bool] = mapped_column(default=False, nullable=False)
    sent_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    delivered_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    safety_state: Mapped[str | None] = mapped_column(String(40))
    repetition_score: Mapped[float | None] = mapped_column()
    prompt_template_name: Mapped[str | None] = mapped_column(String(80))
    tokens_in: Mapped[int | None] = mapped_column(Integer)
    tokens_out: Mapped[int | None] = mapped_column(Integer)
    metadata_json: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict, nullable=False)

    conversation: Mapped[Conversation] = relationship("Conversation", back_populates="messages")
    user: Mapped[User] = relationship("User", back_populates="messages")
    persona: Mapped[Persona | None] = relationship("Persona", back_populates="messages")
    media_assets: Mapped[list[MediaAsset]] = relationship("MediaAsset", back_populates="message")
    delivery_attempts: Mapped[list[DeliveryAttempt]] = relationship("DeliveryAttempt", back_populates="message")
    safety_events: Mapped[list[SafetyEvent]] = relationship("SafetyEvent", back_populates="message")


class MediaAsset(UUIDPrimaryKeyMixin, TimestampMixin, Base):
    __tablename__ = "media_assets"
    __table_args__ = (
        Index("ix_media_assets_user_created", "user_id", "created_at"),
        Index("ix_media_assets_status", "generation_status"),
    )

    message_id: Mapped[uuid.UUID | None] = mapped_column(ForeignKey("messages.id"))
    user_id: Mapped[uuid.UUID | None] = mapped_column(ForeignKey("users.id"))
    persona_id: Mapped[uuid.UUID | None] = mapped_column(ForeignKey("personas.id"))
    provider_asset_id: Mapped[str | None] = mapped_column(String(120))
    role: Mapped[MediaRole] = mapped_column(Enum(MediaRole, values_callable=enum_values), nullable=False)
    mime_type: Mapped[str | None] = mapped_column(String(120))
    local_path: Mapped[str | None] = mapped_column(String(300))
    remote_url: Mapped[str | None] = mapped_column(String(500))
    prompt_text: Mapped[str | None] = mapped_column(Text())
    negative_prompt: Mapped[str | None] = mapped_column(Text())
    generation_status: Mapped[str] = mapped_column(String(40), default="ready", nullable=False)
    error_message: Mapped[str | None] = mapped_column(Text())
    metadata_json: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict, nullable=False)

    message: Mapped[Message | None] = relationship("Message", back_populates="media_assets")
    user: Mapped[User | None] = relationship("User", back_populates="media_assets")
    persona: Mapped[Persona | None] = relationship("Persona", back_populates="media_assets")


class DeliveryAttempt(UUIDPrimaryKeyMixin, TimestampMixin, Base):
    __tablename__ = "delivery_attempts"
    __table_args__ = (
        Index("ix_delivery_attempts_message_created", "message_id", "created_at"),
        Index("ix_delivery_attempts_status", "status"),
    )

    message_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("messages.id"), nullable=False)
    provider: Mapped[str] = mapped_column(String(40), nullable=False)
    attempt_number: Mapped[int] = mapped_column(Integer, default=1, nullable=False)
    status: Mapped[DeliveryStatus] = mapped_column(Enum(DeliveryStatus, values_callable=enum_values), nullable=False)
    error_message: Mapped[str | None] = mapped_column(Text())
    request_json: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict, nullable=False)
    response_json: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict, nullable=False)

    message: Mapped[Message] = relationship("Message", back_populates="delivery_attempts")


class SafetyEvent(UUIDPrimaryKeyMixin, TimestampMixin, Base):
    __tablename__ = "safety_events"
    __table_args__ = (
        Index("ix_safety_events_user_created", "user_id", "created_at"),
        Index("ix_safety_events_severity", "severity"),
    )

    user_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("users.id"), nullable=False)
    persona_id: Mapped[uuid.UUID | None] = mapped_column(ForeignKey("personas.id"))
    conversation_id: Mapped[uuid.UUID | None] = mapped_column(ForeignKey("conversations.id"))
    message_id: Mapped[uuid.UUID | None] = mapped_column(ForeignKey("messages.id"))
    event_type: Mapped[str] = mapped_column(String(80), nullable=False)
    severity: Mapped[SafetySeverity] = mapped_column(Enum(SafetySeverity, values_callable=enum_values), nullable=False)
    detector: Mapped[str] = mapped_column(String(80), nullable=False)
    action_taken: Mapped[str | None] = mapped_column(Text())
    acknowledged_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    details_json: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict, nullable=False)

    user: Mapped[User] = relationship("User", back_populates="safety_events")
    persona: Mapped[Persona | None] = relationship("Persona", back_populates="safety_events")
    message: Mapped[Message | None] = relationship("Message", back_populates="safety_events")


class CallRecord(UUIDPrimaryKeyMixin, TimestampMixin, Base):
    __tablename__ = "call_records"
    __table_args__ = (
        Index("ix_call_records_provider_sid", "provider_call_sid", unique=True),
        Index("ix_call_records_user_created", "user_id", "created_at"),
    )

    user_id: Mapped[uuid.UUID | None] = mapped_column(ForeignKey("users.id"))
    persona_id: Mapped[uuid.UUID | None] = mapped_column(ForeignKey("personas.id"))
    provider_call_sid: Mapped[str | None] = mapped_column(String(80))
    direction: Mapped[CallDirection] = mapped_column(Enum(CallDirection, values_callable=enum_values), nullable=False)
    status: Mapped[CallStatus] = mapped_column(Enum(CallStatus, values_callable=enum_values), nullable=False)
    from_number: Mapped[str | None] = mapped_column(String(32))
    to_number: Mapped[str | None] = mapped_column(String(32))
    script: Mapped[str | None] = mapped_column(Text())
    transcript: Mapped[str | None] = mapped_column(Text())
    duration_seconds: Mapped[int | None] = mapped_column(Integer)
    started_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    ended_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    metadata_json: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict, nullable=False)

    user: Mapped[User | None] = relationship("User", back_populates="call_records")
    persona: Mapped[Persona | None] = relationship("Persona", back_populates="call_records")
