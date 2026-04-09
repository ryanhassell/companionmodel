"""add portal parent chat threads and messages

Revision ID: 20260409_0008
Revises: 20260409_0007
Create Date: 2026-04-09 14:45:00
"""

from __future__ import annotations

from alembic import op
import sqlalchemy as sa


revision = "20260409_0008"
down_revision = "20260409_0007"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "portal_chat_threads",
        sa.Column("id", sa.UUID(), primary_key=True),
        sa.Column("account_id", sa.UUID(), sa.ForeignKey("accounts.id"), nullable=False),
        sa.Column("customer_user_id", sa.UUID(), sa.ForeignKey("customer_users.id"), nullable=False),
        sa.Column("child_profile_id", sa.UUID(), sa.ForeignKey("child_profiles.id"), nullable=False),
        sa.Column("status", sa.String(length=32), nullable=False, server_default="open"),
        sa.Column("metadata_json", sa.JSON(), nullable=False, server_default="{}"),
        sa.Column("last_parent_message_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("last_assistant_message_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
    )
    op.create_index(
        "ix_portal_chat_threads_customer_child",
        "portal_chat_threads",
        ["customer_user_id", "child_profile_id"],
        unique=True,
    )
    op.create_index(
        "ix_portal_chat_threads_account_updated",
        "portal_chat_threads",
        ["account_id", "updated_at"],
        unique=False,
    )

    op.create_table(
        "portal_chat_messages",
        sa.Column("id", sa.UUID(), primary_key=True),
        sa.Column("thread_id", sa.UUID(), sa.ForeignKey("portal_chat_threads.id", ondelete="CASCADE"), nullable=False),
        sa.Column("sender", sa.String(length=20), nullable=False),
        sa.Column("body", sa.Text(), nullable=False),
        sa.Column("metadata_json", sa.JSON(), nullable=False, server_default="{}"),
        sa.Column("model_name", sa.String(length=120), nullable=True),
        sa.Column("tokens_in", sa.Integer(), nullable=True),
        sa.Column("tokens_out", sa.Integer(), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
    )
    op.create_index(
        "ix_portal_chat_messages_thread_created",
        "portal_chat_messages",
        ["thread_id", "created_at"],
        unique=False,
    )


def downgrade() -> None:
    op.drop_index("ix_portal_chat_messages_thread_created", table_name="portal_chat_messages")
    op.drop_table("portal_chat_messages")
    op.drop_index("ix_portal_chat_threads_account_updated", table_name="portal_chat_threads")
    op.drop_index("ix_portal_chat_threads_customer_child", table_name="portal_chat_threads")
    op.drop_table("portal_chat_threads")
