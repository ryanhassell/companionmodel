"""add portal ownership metadata to personas

Revision ID: 20260412_0012
Revises: 20260410_0011
Create Date: 2026-04-12 18:05:00
"""

from __future__ import annotations

from alembic import op
import sqlalchemy as sa


revision = "20260412_0012"
down_revision = "20260410_0011"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column("personas", sa.Column("account_id", sa.UUID(), nullable=True))
    op.add_column("personas", sa.Column("owner_user_id", sa.UUID(), nullable=True))
    op.add_column(
        "personas",
        sa.Column("source_type", sa.String(length=40), nullable=False, server_default="admin"),
    )
    op.add_column("personas", sa.Column("preset_key", sa.String(length=80), nullable=True))

    op.create_foreign_key("fk_personas_account_id_accounts", "personas", "accounts", ["account_id"], ["id"])
    op.create_foreign_key("fk_personas_owner_user_id_users", "personas", "users", ["owner_user_id"], ["id"])
    op.create_index("ix_personas_source", "personas", ["source_type", "updated_at"], unique=False)
    op.create_index("ix_personas_account_owner", "personas", ["account_id", "owner_user_id"], unique=False)


def downgrade() -> None:
    op.drop_index("ix_personas_account_owner", table_name="personas")
    op.drop_index("ix_personas_source", table_name="personas")
    op.drop_constraint("fk_personas_owner_user_id_users", "personas", type_="foreignkey")
    op.drop_constraint("fk_personas_account_id_accounts", "personas", type_="foreignkey")
    op.drop_column("personas", "preset_key")
    op.drop_column("personas", "source_type")
    op.drop_column("personas", "owner_user_id")
    op.drop_column("personas", "account_id")
