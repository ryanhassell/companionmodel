"""add clerk identity link fields

Revision ID: 20260407_0004
Revises: 20260407_0003
Create Date: 2026-04-07 00:40:00
"""

from __future__ import annotations

from alembic import op
import sqlalchemy as sa


revision = "20260407_0004"
down_revision = "20260407_0003"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column("accounts", sa.Column("clerk_org_id", sa.String(length=120), nullable=True))
    op.create_index("ix_accounts_clerk_org_id", "accounts", ["clerk_org_id"], unique=True)

    op.add_column("customer_users", sa.Column("clerk_user_id", sa.String(length=120), nullable=True))
    op.add_column("customer_users", sa.Column("last_clerk_auth_at", sa.DateTime(timezone=True), nullable=True))
    op.create_index("ix_customer_users_clerk_user_id", "customer_users", ["clerk_user_id"], unique=True)

    op.create_table(
        "auth_identity_events",
        sa.Column("id", sa.UUID(), primary_key=True),
        sa.Column("account_id", sa.UUID(), sa.ForeignKey("accounts.id")),
        sa.Column("customer_user_id", sa.UUID(), sa.ForeignKey("customer_users.id")),
        sa.Column("event_type", sa.String(length=80), nullable=False),
        sa.Column("details_json", sa.JSON(), nullable=False, server_default="{}"),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
    )
    op.create_index("ix_auth_identity_events_account_created", "auth_identity_events", ["account_id", "created_at"])
    op.create_index("ix_auth_identity_events_user_created", "auth_identity_events", ["customer_user_id", "created_at"])


def downgrade() -> None:
    op.drop_index("ix_auth_identity_events_user_created", table_name="auth_identity_events")
    op.drop_index("ix_auth_identity_events_account_created", table_name="auth_identity_events")
    op.drop_table("auth_identity_events")

    op.drop_index("ix_customer_users_clerk_user_id", table_name="customer_users")
    op.drop_column("customer_users", "last_clerk_auth_at")
    op.drop_column("customer_users", "clerk_user_id")

    op.drop_index("ix_accounts_clerk_org_id", table_name="accounts")
    op.drop_column("accounts", "clerk_org_id")
