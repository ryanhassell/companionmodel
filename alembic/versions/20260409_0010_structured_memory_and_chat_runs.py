"""add structured memory entities and portal chat runs

Revision ID: 20260409_0010
Revises: 20260409_0009
Create Date: 2026-04-09 20:10:00
"""

from __future__ import annotations

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql


revision = "20260409_0010"
down_revision = "20260409_0009"
branch_labels = None
depends_on = None


memory_entity_kind = sa.Enum(
    "child",
    "family_member",
    "friend",
    "pet",
    "artist",
    "activity",
    "routine_anchor",
    "event",
    "health_context",
    "topic",
    name="memoryentitykind",
    native_enum=False,
    length=32,
)

memory_facet = sa.Enum(
    "identity",
    "family",
    "friends",
    "pets",
    "interests",
    "favorites",
    "routines",
    "milestones",
    "health_context",
    "preferences",
    "events",
    name="memoryfacet",
    native_enum=False,
    length=32,
)

entity_relation_kind = sa.Enum(
    "child_world",
    "family_member",
    "friend",
    "pet",
    "favorite",
    "interest",
    "routine",
    "related",
    name="entityrelationkind",
    native_enum=False,
    length=32,
)

portal_chat_run_status = postgresql.ENUM(
    "running",
    "completed",
    "failed",
    name="portalchatrunstatus",
    create_type=False,
)

portal_chat_message_kind = postgresql.ENUM(
    "message",
    "activity",
    name="portalchatmessagekind",
    create_type=False,
)


def upgrade() -> None:
    bind = op.get_bind()
    memory_entity_kind.create(bind, checkfirst=True)
    memory_facet.create(bind, checkfirst=True)
    entity_relation_kind.create(bind, checkfirst=True)
    portal_chat_run_status.create(bind, checkfirst=True)
    portal_chat_message_kind.create(bind, checkfirst=True)

    op.create_table(
        "memory_entities",
        sa.Column("id", sa.UUID(), primary_key=True),
        sa.Column("user_id", sa.UUID(), sa.ForeignKey("users.id", ondelete="CASCADE"), nullable=False),
        sa.Column("persona_id", sa.UUID(), sa.ForeignKey("personas.id", ondelete="SET NULL"), nullable=True),
        sa.Column("display_name", sa.String(length=160), nullable=False),
        sa.Column("normalized_name", sa.String(length=160), nullable=False),
        sa.Column("entity_kind", memory_entity_kind, nullable=False),
        sa.Column("default_facet", memory_facet, nullable=False, server_default="identity"),
        sa.Column("relation_to_child", sa.String(length=80), nullable=True),
        sa.Column("provenance_source", sa.String(length=80), nullable=True),
        sa.Column("canonical_value", sa.Text(), nullable=True),
        sa.Column("is_primary", sa.Boolean(), nullable=False, server_default=sa.false()),
        sa.Column("metadata_json", sa.JSON(), nullable=False, server_default="{}"),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
    )
    op.create_index("ix_memory_entities_user_kind", "memory_entities", ["user_id", "entity_kind"], unique=False)
    op.create_index("ix_memory_entities_user_name", "memory_entities", ["user_id", "normalized_name"], unique=False)
    op.create_index("ix_memory_entities_user_primary", "memory_entities", ["user_id", "is_primary"], unique=False)

    op.create_table(
        "memory_entity_relations",
        sa.Column("id", sa.UUID(), primary_key=True),
        sa.Column("user_id", sa.UUID(), sa.ForeignKey("users.id", ondelete="CASCADE"), nullable=False),
        sa.Column("parent_entity_id", sa.UUID(), sa.ForeignKey("memory_entities.id", ondelete="CASCADE"), nullable=False),
        sa.Column("child_entity_id", sa.UUID(), sa.ForeignKey("memory_entities.id", ondelete="CASCADE"), nullable=False),
        sa.Column("relationship_kind", entity_relation_kind, nullable=False),
        sa.Column("metadata_json", sa.JSON(), nullable=False, server_default="{}"),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
    )
    op.create_index("ix_memory_entity_relations_user_parent", "memory_entity_relations", ["user_id", "parent_entity_id"], unique=False)
    op.create_index("ix_memory_entity_relations_user_child", "memory_entity_relations", ["user_id", "child_entity_id"], unique=False)
    op.create_index(
        "ix_memory_entity_relations_unique",
        "memory_entity_relations",
        ["user_id", "parent_entity_id", "child_entity_id", "relationship_kind"],
        unique=True,
    )

    op.create_table(
        "memory_item_entities",
        sa.Column("id", sa.UUID(), primary_key=True),
        sa.Column("memory_id", sa.UUID(), sa.ForeignKey("memory_items.id", ondelete="CASCADE"), nullable=False),
        sa.Column("entity_id", sa.UUID(), sa.ForeignKey("memory_entities.id", ondelete="CASCADE"), nullable=False),
        sa.Column("role", sa.String(length=32), nullable=False, server_default="primary"),
        sa.Column("facet", memory_facet, nullable=False, server_default="identity"),
        sa.Column("is_primary", sa.Boolean(), nullable=False, server_default=sa.false()),
        sa.Column("metadata_json", sa.JSON(), nullable=False, server_default="{}"),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
    )
    op.create_index("ix_memory_item_entities_memory", "memory_item_entities", ["memory_id"], unique=False)
    op.create_index("ix_memory_item_entities_entity", "memory_item_entities", ["entity_id"], unique=False)
    op.create_index(
        "ix_memory_item_entities_unique",
        "memory_item_entities",
        ["memory_id", "entity_id", "role"],
        unique=True,
    )

    op.create_table(
        "portal_chat_runs",
        sa.Column("id", sa.UUID(), primary_key=True),
        sa.Column("account_id", sa.UUID(), sa.ForeignKey("accounts.id"), nullable=False),
        sa.Column("customer_user_id", sa.UUID(), sa.ForeignKey("customer_users.id"), nullable=False),
        sa.Column("child_profile_id", sa.UUID(), sa.ForeignKey("child_profiles.id"), nullable=False),
        sa.Column("thread_id", sa.UUID(), sa.ForeignKey("portal_chat_threads.id", ondelete="CASCADE"), nullable=False),
        sa.Column("status", portal_chat_run_status, nullable=False, server_default="running"),
        sa.Column("metadata_json", sa.JSON(), nullable=False, server_default="{}"),
        sa.Column("model_name", sa.String(length=120), nullable=True),
        sa.Column("error_code", sa.String(length=80), nullable=True),
        sa.Column("started_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("completed_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
    )
    op.create_index("ix_portal_chat_runs_thread_created", "portal_chat_runs", ["thread_id", "created_at"], unique=False)
    op.create_index("ix_portal_chat_runs_account_status", "portal_chat_runs", ["account_id", "status"], unique=False)

    op.add_column("portal_chat_messages", sa.Column("run_id", sa.UUID(), nullable=True))
    op.add_column(
        "portal_chat_messages",
        sa.Column("message_kind", portal_chat_message_kind, nullable=False, server_default="message"),
    )
    op.create_foreign_key(
        "fk_portal_chat_messages_run_id",
        "portal_chat_messages",
        "portal_chat_runs",
        ["run_id"],
        ["id"],
        ondelete="SET NULL",
    )
    op.create_index("ix_portal_chat_messages_run_created", "portal_chat_messages", ["run_id", "created_at"], unique=False)


def downgrade() -> None:
    op.drop_index("ix_portal_chat_messages_run_created", table_name="portal_chat_messages")
    op.drop_constraint("fk_portal_chat_messages_run_id", "portal_chat_messages", type_="foreignkey")
    op.drop_column("portal_chat_messages", "message_kind")
    op.drop_column("portal_chat_messages", "run_id")

    op.drop_index("ix_portal_chat_runs_account_status", table_name="portal_chat_runs")
    op.drop_index("ix_portal_chat_runs_thread_created", table_name="portal_chat_runs")
    op.drop_table("portal_chat_runs")

    op.drop_index("ix_memory_item_entities_unique", table_name="memory_item_entities")
    op.drop_index("ix_memory_item_entities_entity", table_name="memory_item_entities")
    op.drop_index("ix_memory_item_entities_memory", table_name="memory_item_entities")
    op.drop_table("memory_item_entities")

    op.drop_index("ix_memory_entity_relations_unique", table_name="memory_entity_relations")
    op.drop_index("ix_memory_entity_relations_user_child", table_name="memory_entity_relations")
    op.drop_index("ix_memory_entity_relations_user_parent", table_name="memory_entity_relations")
    op.drop_table("memory_entity_relations")

    op.drop_index("ix_memory_entities_user_primary", table_name="memory_entities")
    op.drop_index("ix_memory_entities_user_name", table_name="memory_entities")
    op.drop_index("ix_memory_entities_user_kind", table_name="memory_entities")
    op.drop_table("memory_entities")

    bind = op.get_bind()
    portal_chat_message_kind.drop(bind, checkfirst=True)
    portal_chat_run_status.drop(bind, checkfirst=True)
    entity_relation_kind.drop(bind, checkfirst=True)
    memory_facet.drop(bind, checkfirst=True)
    memory_entity_kind.drop(bind, checkfirst=True)
