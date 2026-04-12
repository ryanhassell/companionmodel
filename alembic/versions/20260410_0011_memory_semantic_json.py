"""add semantic json columns to structured memory tables

Revision ID: 20260410_0011
Revises: 20260409_0010
Create Date: 2026-04-10 17:20:00
"""

from __future__ import annotations

from alembic import op
import sqlalchemy as sa


revision = "20260410_0011"
down_revision = "20260409_0010"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "memory_entities",
        sa.Column("semantic_json", sa.JSON(), nullable=False, server_default="{}"),
    )
    op.add_column(
        "memory_entity_relations",
        sa.Column("semantic_json", sa.JSON(), nullable=False, server_default="{}"),
    )
    op.add_column(
        "memory_item_entities",
        sa.Column("semantic_json", sa.JSON(), nullable=False, server_default="{}"),
    )


def downgrade() -> None:
    op.drop_column("memory_item_entities", "semantic_json")
    op.drop_column("memory_entity_relations", "semantic_json")
    op.drop_column("memory_entities", "semantic_json")
