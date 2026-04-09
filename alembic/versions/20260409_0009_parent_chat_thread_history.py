"""allow multiple parent chat threads per child

Revision ID: 20260409_0009
Revises: 20260409_0008
Create Date: 2026-04-09 17:05:00
"""

from __future__ import annotations

from alembic import op


revision = "20260409_0009"
down_revision = "20260409_0008"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.drop_index("ix_portal_chat_threads_customer_child", table_name="portal_chat_threads")
    op.create_index(
        "ix_portal_chat_threads_customer_child",
        "portal_chat_threads",
        ["customer_user_id", "child_profile_id"],
        unique=False,
    )


def downgrade() -> None:
    op.drop_index("ix_portal_chat_threads_customer_child", table_name="portal_chat_threads")
    op.create_index(
        "ix_portal_chat_threads_customer_child",
        "portal_chat_threads",
        ["customer_user_id", "child_profile_id"],
        unique=True,
    )
