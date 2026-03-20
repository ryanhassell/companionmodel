from __future__ import annotations

import uuid
from typing import TYPE_CHECKING, Any

from sqlalchemy import Boolean, Enum, ForeignKey, Index, Integer, JSON, String, Text, Time
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.db.base import Base, TimestampMixin, UUIDPrimaryKeyMixin
from app.models.enums import AppSettingScope, ScheduleRuleType

if TYPE_CHECKING:
    from app.models.persona import Persona
    from app.models.user import User


def enum_values(enum_cls):
    return [member.value for member in enum_cls]


class ScheduleRule(UUIDPrimaryKeyMixin, TimestampMixin, Base):
    __tablename__ = "schedule_rules"
    __table_args__ = (
        Index("ix_schedule_rules_scope", "user_id", "persona_id", "enabled"),
        Index("ix_schedule_rules_type", "rule_type"),
    )

    user_id: Mapped[uuid.UUID | None] = mapped_column(ForeignKey("users.id"))
    persona_id: Mapped[uuid.UUID | None] = mapped_column(ForeignKey("personas.id"))
    name: Mapped[str] = mapped_column(String(120), nullable=False)
    rule_type: Mapped[ScheduleRuleType] = mapped_column(
        Enum(ScheduleRuleType, values_callable=enum_values),
        nullable=False,
    )
    weekday: Mapped[int | None] = mapped_column(Integer)
    start_time: Mapped[Any | None] = mapped_column(Time(timezone=False))
    end_time: Mapped[Any | None] = mapped_column(Time(timezone=False))
    min_gap_minutes: Mapped[int | None] = mapped_column(Integer)
    max_gap_minutes: Mapped[int | None] = mapped_column(Integer)
    probability: Mapped[float | None] = mapped_column()
    priority: Mapped[int] = mapped_column(Integer, default=100, nullable=False)
    config_json: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict, nullable=False)
    enabled: Mapped[bool] = mapped_column(Boolean, default=True, nullable=False)

    user: Mapped[User | None] = relationship("User", back_populates="schedule_rules")
    persona: Mapped[Persona | None] = relationship("Persona", back_populates="schedule_rules")


class AppSetting(UUIDPrimaryKeyMixin, TimestampMixin, Base):
    __tablename__ = "app_settings"
    __table_args__ = (
        Index("ix_app_settings_scope_key", "scope", "namespace", "key"),
        Index("ix_app_settings_user_scope", "user_id", "persona_id"),
    )

    scope: Mapped[AppSettingScope] = mapped_column(
        Enum(AppSettingScope, values_callable=enum_values),
        nullable=False,
    )
    namespace: Mapped[str] = mapped_column(String(80), nullable=False)
    key: Mapped[str] = mapped_column(String(80), nullable=False)
    description: Mapped[str | None] = mapped_column(Text())
    value_json: Mapped[dict[str, Any] | list[Any] | str | int | float | bool | None] = mapped_column(
        JSON,
        nullable=True,
    )
    user_id: Mapped[uuid.UUID | None] = mapped_column(ForeignKey("users.id"))
    persona_id: Mapped[uuid.UUID | None] = mapped_column(ForeignKey("personas.id"))

    user: Mapped[User | None] = relationship("User", back_populates="settings")
    persona: Mapped[Persona | None] = relationship("Persona", back_populates="settings")


class PromptTemplate(UUIDPrimaryKeyMixin, TimestampMixin, Base):
    __tablename__ = "prompt_templates"
    __table_args__ = (
        Index("ix_prompt_templates_name_version", "name", "version", unique=True),
        Index("ix_prompt_templates_active", "name", "is_active"),
    )

    name: Mapped[str] = mapped_column(String(80), nullable=False)
    channel: Mapped[str] = mapped_column(String(40), default="sms", nullable=False)
    description: Mapped[str | None] = mapped_column(Text())
    version: Mapped[int] = mapped_column(default=1, nullable=False)
    body: Mapped[str] = mapped_column(Text(), nullable=False)
    variables_json: Mapped[list[str]] = mapped_column(JSON, default=list, nullable=False)
    source: Mapped[str] = mapped_column(String(32), default="file_seed", nullable=False)
    is_active: Mapped[bool] = mapped_column(Boolean, default=True, nullable=False)
    locked: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)
