from __future__ import annotations

import uuid
from datetime import datetime
from typing import TYPE_CHECKING, Any

from sqlalchemy import DateTime, Enum, ForeignKey, Index, Integer, JSON, String, Text
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.db.base import Base, TimestampMixin, UUIDPrimaryKeyMixin
from app.db.types import EmbeddingVectorType
from app.models.enums import EntityRelationKind, MemoryEntityKind, MemoryFacet, MemoryRelationshipType, MemoryType

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
    entity_links: Mapped[list[MemoryItemEntity]] = relationship(
        "MemoryItemEntity",
        back_populates="memory",
        cascade="all, delete-orphan",
    )


class MemoryRelationship(UUIDPrimaryKeyMixin, TimestampMixin, Base):
    __tablename__ = "memory_relationships"
    __table_args__ = (
        Index("ix_memory_relationships_user_parent", "user_id", "parent_memory_id"),
        Index("ix_memory_relationships_user_child", "user_id", "child_memory_id"),
        Index(
            "ix_memory_relationships_unique",
            "user_id",
            "parent_memory_id",
            "child_memory_id",
            "relationship_type",
            unique=True,
        ),
    )

    user_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("users.id", ondelete="CASCADE"), nullable=False)
    parent_memory_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("memory_items.id", ondelete="CASCADE"),
        nullable=False,
    )
    child_memory_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("memory_items.id", ondelete="CASCADE"),
        nullable=False,
    )
    relationship_type: Mapped[MemoryRelationshipType] = mapped_column(
        Enum(
            MemoryRelationshipType,
            values_callable=enum_values,
            native_enum=False,
            length=24,
        ),
        nullable=False,
    )
    metadata_json: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict, nullable=False)

    user: Mapped[User | None] = relationship("User")
    parent_memory: Mapped[MemoryItem | None] = relationship("MemoryItem", foreign_keys=[parent_memory_id])
    child_memory: Mapped[MemoryItem | None] = relationship("MemoryItem", foreign_keys=[child_memory_id])


class MemoryEntity(UUIDPrimaryKeyMixin, TimestampMixin, Base):
    __tablename__ = "memory_entities"
    __table_args__ = (
        Index("ix_memory_entities_user_kind", "user_id", "entity_kind"),
        Index("ix_memory_entities_user_name", "user_id", "normalized_name"),
        Index("ix_memory_entities_user_primary", "user_id", "is_primary"),
    )

    user_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("users.id", ondelete="CASCADE"), nullable=False)
    persona_id: Mapped[uuid.UUID | None] = mapped_column(ForeignKey("personas.id", ondelete="SET NULL"))
    display_name: Mapped[str] = mapped_column(String(160), nullable=False)
    normalized_name: Mapped[str] = mapped_column(String(160), nullable=False)
    entity_kind: Mapped[MemoryEntityKind] = mapped_column(
        Enum(MemoryEntityKind, values_callable=enum_values, native_enum=False, length=32),
        nullable=False,
    )
    default_facet: Mapped[MemoryFacet] = mapped_column(
        Enum(MemoryFacet, values_callable=enum_values, native_enum=False, length=32),
        nullable=False,
        default=MemoryFacet.identity,
    )
    relation_to_child: Mapped[str | None] = mapped_column(String(80))
    provenance_source: Mapped[str | None] = mapped_column(String(80))
    canonical_value: Mapped[str | None] = mapped_column(Text())
    is_primary: Mapped[bool] = mapped_column(default=False, nullable=False)
    semantic_json: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict, nullable=False)
    metadata_json: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict, nullable=False)

    user: Mapped[User | None] = relationship("User")
    persona: Mapped[Persona | None] = relationship("Persona")
    memory_links: Mapped[list[MemoryItemEntity]] = relationship(
        "MemoryItemEntity",
        back_populates="entity",
        cascade="all, delete-orphan",
    )


class MemoryEntityRelation(UUIDPrimaryKeyMixin, TimestampMixin, Base):
    __tablename__ = "memory_entity_relations"
    __table_args__ = (
        Index("ix_memory_entity_relations_user_parent", "user_id", "parent_entity_id"),
        Index("ix_memory_entity_relations_user_child", "user_id", "child_entity_id"),
        Index(
            "ix_memory_entity_relations_unique",
            "user_id",
            "parent_entity_id",
            "child_entity_id",
            "relationship_kind",
            unique=True,
        ),
    )

    user_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("users.id", ondelete="CASCADE"), nullable=False)
    parent_entity_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("memory_entities.id", ondelete="CASCADE"),
        nullable=False,
    )
    child_entity_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("memory_entities.id", ondelete="CASCADE"),
        nullable=False,
    )
    relationship_kind: Mapped[EntityRelationKind] = mapped_column(
        Enum(EntityRelationKind, values_callable=enum_values, native_enum=False, length=32),
        nullable=False,
    )
    semantic_json: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict, nullable=False)
    metadata_json: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict, nullable=False)

    user: Mapped[User | None] = relationship("User")
    parent_entity: Mapped[MemoryEntity | None] = relationship("MemoryEntity", foreign_keys=[parent_entity_id])
    child_entity: Mapped[MemoryEntity | None] = relationship("MemoryEntity", foreign_keys=[child_entity_id])


class MemoryItemEntity(UUIDPrimaryKeyMixin, TimestampMixin, Base):
    __tablename__ = "memory_item_entities"
    __table_args__ = (
        Index("ix_memory_item_entities_memory", "memory_id"),
        Index("ix_memory_item_entities_entity", "entity_id"),
        Index(
            "ix_memory_item_entities_unique",
            "memory_id",
            "entity_id",
            "role",
            unique=True,
        ),
    )

    memory_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("memory_items.id", ondelete="CASCADE"), nullable=False)
    entity_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("memory_entities.id", ondelete="CASCADE"), nullable=False)
    role: Mapped[str] = mapped_column(String(32), nullable=False, default="primary")
    facet: Mapped[MemoryFacet] = mapped_column(
        Enum(MemoryFacet, values_callable=enum_values, native_enum=False, length=32),
        nullable=False,
        default=MemoryFacet.identity,
    )
    is_primary: Mapped[bool] = mapped_column(default=False, nullable=False)
    semantic_json: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict, nullable=False)
    metadata_json: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict, nullable=False)

    memory: Mapped[MemoryItem] = relationship("MemoryItem", back_populates="entity_links")
    entity: Mapped[MemoryEntity] = relationship("MemoryEntity", back_populates="memory_links")
