"""add persisted memory relationships for portal graph

Revision ID: 20260409_0007
Revises: 20260408_0006
Create Date: 2026-04-09 03:50:00
"""

from __future__ import annotations

from alembic import op
import sqlalchemy as sa


revision = "20260409_0007"
down_revision = "20260408_0006"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "memory_relationships",
        sa.Column("id", sa.UUID(), primary_key=True),
        sa.Column("user_id", sa.UUID(), sa.ForeignKey("users.id", ondelete="CASCADE"), nullable=False),
        sa.Column("parent_memory_id", sa.UUID(), sa.ForeignKey("memory_items.id", ondelete="CASCADE"), nullable=False),
        sa.Column("child_memory_id", sa.UUID(), sa.ForeignKey("memory_items.id", ondelete="CASCADE"), nullable=False),
        sa.Column("relationship_type", sa.String(length=24), nullable=False),
        sa.Column("metadata_json", sa.JSON(), nullable=False, server_default="{}"),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
    )
    op.create_index(
        "ix_memory_relationships_user_parent",
        "memory_relationships",
        ["user_id", "parent_memory_id"],
        unique=False,
    )
    op.create_index(
        "ix_memory_relationships_user_child",
        "memory_relationships",
        ["user_id", "child_memory_id"],
        unique=False,
    )
    op.create_index(
        "ix_memory_relationships_unique",
        "memory_relationships",
        ["user_id", "parent_memory_id", "child_memory_id", "relationship_type"],
        unique=True,
    )


def downgrade() -> None:
    op.drop_index("ix_memory_relationships_unique", table_name="memory_relationships")
    op.drop_index("ix_memory_relationships_user_child", table_name="memory_relationships")
    op.drop_index("ix_memory_relationships_user_parent", table_name="memory_relationships")
    op.drop_table("memory_relationships")
