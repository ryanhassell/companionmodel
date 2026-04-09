"""add conversation states

Revision ID: 20260407_0002
Revises: 20260318_0001
Create Date: 2026-04-07 00:00:00
"""

from __future__ import annotations

from alembic import op
import sqlalchemy as sa


revision = "20260407_0002"
down_revision = "20260318_0001"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "conversation_states",
        sa.Column("id", sa.UUID(), primary_key=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
        sa.Column("conversation_id", sa.UUID(), sa.ForeignKey("conversations.id"), nullable=False),
        sa.Column("user_id", sa.UUID(), sa.ForeignKey("users.id"), nullable=False),
        sa.Column("persona_id", sa.UUID(), sa.ForeignKey("personas.id")),
        sa.Column("active_topics", sa.JSON(), nullable=False, server_default="[]"),
        sa.Column("open_loops", sa.JSON(), nullable=False, server_default="[]"),
        sa.Column("last_user_questions", sa.JSON(), nullable=False, server_default="[]"),
        sa.Column("recent_mood_trend", sa.String(length=40)),
        sa.Column("style_fingerprint", sa.JSON(), nullable=False, server_default="{}"),
        sa.Column("boundary_pressure_score", sa.Float(), nullable=False, server_default="0"),
        sa.Column("novelty_budget", sa.Float(), nullable=False, server_default="1"),
        sa.Column("fatigue_score", sa.Float(), nullable=False, server_default="0"),
        sa.Column("last_archetype", sa.String(length=40)),
        sa.Column("continuity_card", sa.Text()),
        sa.Column("relationship_milestones", sa.JSON(), nullable=False, server_default="[]"),
        sa.Column("metadata_json", sa.JSON(), nullable=False, server_default="{}"),
    )
    op.create_index(
        "ix_conversation_states_conversation",
        "conversation_states",
        ["conversation_id"],
        unique=True,
    )
    op.create_index(
        "ix_conversation_states_user_persona",
        "conversation_states",
        ["user_id", "persona_id"],
    )


def downgrade() -> None:
    op.drop_index("ix_conversation_states_user_persona", table_name="conversation_states")
    op.drop_index("ix_conversation_states_conversation", table_name="conversation_states")
    op.drop_table("conversation_states")
