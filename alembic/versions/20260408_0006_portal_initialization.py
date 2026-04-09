"""add account initialization state for parent portal

Revision ID: 20260408_0006
Revises: 20260408_0005
Create Date: 2026-04-08 20:10:00
"""

from __future__ import annotations

from alembic import op
import sqlalchemy as sa


revision = "20260408_0006"
down_revision = "20260408_0005"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "account_initializations",
        sa.Column("id", sa.UUID(), primary_key=True),
        sa.Column("account_id", sa.UUID(), sa.ForeignKey("accounts.id"), nullable=False),
        sa.Column("status", sa.String(length=24), nullable=False, server_default="in_progress"),
        sa.Column("current_step", sa.String(length=40), nullable=False, server_default="welcome"),
        sa.Column("completed_steps_json", sa.JSON(), nullable=False, server_default="[]"),
        sa.Column("selected_plan_key", sa.String(length=24), nullable=True),
        sa.Column("snapshot_json", sa.JSON(), nullable=False, server_default="{}"),
        sa.Column("started_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
        sa.Column("completed_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
    )
    op.create_index("ix_account_initializations_account", "account_initializations", ["account_id"], unique=True)
    op.create_index("ix_account_initializations_status", "account_initializations", ["status", "updated_at"])


def downgrade() -> None:
    op.drop_index("ix_account_initializations_status", table_name="account_initializations")
    op.drop_index("ix_account_initializations_account", table_name="account_initializations")
    op.drop_table("account_initializations")
