"""add usage economics ledger and clerk admin identities

Revision ID: 20260408_0005
Revises: 20260407_0004
Create Date: 2026-04-08 19:10:00
"""

from __future__ import annotations

from alembic import op
import sqlalchemy as sa


revision = "20260408_0005"
down_revision = "20260407_0004"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "admin_identities",
        sa.Column("id", sa.UUID(), primary_key=True),
        sa.Column("admin_user_id", sa.UUID(), sa.ForeignKey("admin_users.id"), nullable=False),
        sa.Column("provider", sa.String(length=40), nullable=False, server_default="clerk"),
        sa.Column("clerk_user_id", sa.String(length=120), nullable=False),
        sa.Column("clerk_org_id", sa.String(length=120), nullable=True),
        sa.Column("email", sa.String(length=255), nullable=True),
        sa.Column("org_role", sa.String(length=80), nullable=True),
        sa.Column("mfa_verified", sa.Boolean(), nullable=False, server_default=sa.false()),
        sa.Column("allowlisted", sa.Boolean(), nullable=False, server_default=sa.false()),
        sa.Column("is_active", sa.Boolean(), nullable=False, server_default=sa.true()),
        sa.Column("last_auth_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("metadata_json", sa.JSON(), nullable=False, server_default="{}"),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
    )
    op.create_index("ix_admin_identities_admin_user", "admin_identities", ["admin_user_id"])
    op.create_index("ix_admin_identities_clerk_user_id", "admin_identities", ["clerk_user_id"], unique=True)
    op.create_index("ix_admin_identities_email", "admin_identities", ["email"])

    op.create_table(
        "usage_events",
        sa.Column("id", sa.UUID(), primary_key=True),
        sa.Column("account_id", sa.UUID(), sa.ForeignKey("accounts.id"), nullable=False),
        sa.Column("user_id", sa.UUID(), sa.ForeignKey("users.id"), nullable=True),
        sa.Column("conversation_id", sa.UUID(), sa.ForeignKey("conversations.id"), nullable=True),
        sa.Column("provider", sa.String(length=40), nullable=False),
        sa.Column("product_surface", sa.String(length=40), nullable=False),
        sa.Column("event_type", sa.String(length=80), nullable=False),
        sa.Column("external_id", sa.String(length=120), nullable=True),
        sa.Column("idempotency_key", sa.String(length=180), nullable=False),
        sa.Column("quantity", sa.Float(), nullable=False, server_default="0"),
        sa.Column("unit", sa.String(length=32), nullable=False, server_default="unit"),
        sa.Column("cost_usd", sa.Float(), nullable=True),
        sa.Column("currency", sa.String(length=8), nullable=False, server_default="usd"),
        sa.Column("pricing_state", sa.String(length=20), nullable=False, server_default="pending"),
        sa.Column("estimated_cost_usd", sa.Float(), nullable=True),
        sa.Column("estimated_vs_final_delta", sa.Float(), nullable=True),
        sa.Column("source_ref", sa.String(length=255), nullable=True),
        sa.Column("occurred_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
        sa.Column("reconciled_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("metadata_json", sa.JSON(), nullable=False, server_default="{}"),
    )
    op.create_index("ix_usage_events_account_created", "usage_events", ["account_id", "occurred_at"])
    op.create_index("ix_usage_events_provider_event", "usage_events", ["provider", "event_type"])
    op.create_index("ix_usage_events_idempotency", "usage_events", ["idempotency_key"], unique=True)
    op.create_index("ix_usage_events_external", "usage_events", ["provider", "external_id"])

    op.create_table(
        "usage_reconciliation_runs",
        sa.Column("id", sa.UUID(), primary_key=True),
        sa.Column("status", sa.String(length=20), nullable=False, server_default="running"),
        sa.Column("provider", sa.String(length=40), nullable=False, server_default="all"),
        sa.Column("started_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
        sa.Column("finished_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("finalized_count", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("pending_count", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("failed_count", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("details_json", sa.JSON(), nullable=False, server_default="{}"),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
    )
    op.create_index("ix_usage_reconciliation_runs_created", "usage_reconciliation_runs", ["created_at"])
    op.create_index("ix_usage_reconciliation_runs_status", "usage_reconciliation_runs", ["status"])

    op.create_table(
        "plan_simulation_runs",
        sa.Column("id", sa.UUID(), primary_key=True),
        sa.Column("profile", sa.String(length=40), nullable=False, server_default="real_family_usage"),
        sa.Column("actor_count", sa.Integer(), nullable=False, server_default="100"),
        sa.Column("period_days", sa.Integer(), nullable=False, server_default="30"),
        sa.Column("baseline_chat_price_usd", sa.Float(), nullable=False, server_default="24"),
        sa.Column("baseline_voice_price_usd", sa.Float(), nullable=False, server_default="59"),
        sa.Column("details_json", sa.JSON(), nullable=False, server_default="{}"),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
    )
    op.create_index("ix_plan_simulation_runs_created", "plan_simulation_runs", ["created_at"])

    op.create_table(
        "plan_simulation_scenarios",
        sa.Column("id", sa.UUID(), primary_key=True),
        sa.Column("simulation_run_id", sa.UUID(), sa.ForeignKey("plan_simulation_runs.id"), nullable=False),
        sa.Column("name", sa.String(length=80), nullable=False),
        sa.Column("plan_chat_price_usd", sa.Float(), nullable=False, server_default="24"),
        sa.Column("plan_voice_price_usd", sa.Float(), nullable=False, server_default="59"),
        sa.Column("included_chat_credits_usd", sa.Float(), nullable=False, server_default="8"),
        sa.Column("included_voice_credits_usd", sa.Float(), nullable=False, server_default="28"),
        sa.Column("projected_revenue_usd", sa.Float(), nullable=False, server_default="0"),
        sa.Column("projected_cost_usd", sa.Float(), nullable=False, server_default="0"),
        sa.Column("projected_margin_pct", sa.Float(), nullable=False, server_default="0"),
        sa.Column("recommendation_band", sa.String(length=20), nullable=False, server_default="tight"),
        sa.Column("details_json", sa.JSON(), nullable=False, server_default="{}"),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
    )
    op.create_index("ix_plan_simulation_scenarios_run", "plan_simulation_scenarios", ["simulation_run_id"])


def downgrade() -> None:
    op.drop_index("ix_plan_simulation_scenarios_run", table_name="plan_simulation_scenarios")
    op.drop_table("plan_simulation_scenarios")

    op.drop_index("ix_plan_simulation_runs_created", table_name="plan_simulation_runs")
    op.drop_table("plan_simulation_runs")

    op.drop_index("ix_usage_reconciliation_runs_status", table_name="usage_reconciliation_runs")
    op.drop_index("ix_usage_reconciliation_runs_created", table_name="usage_reconciliation_runs")
    op.drop_table("usage_reconciliation_runs")

    op.drop_index("ix_usage_events_external", table_name="usage_events")
    op.drop_index("ix_usage_events_idempotency", table_name="usage_events")
    op.drop_index("ix_usage_events_provider_event", table_name="usage_events")
    op.drop_index("ix_usage_events_account_created", table_name="usage_events")
    op.drop_table("usage_events")

    op.drop_index("ix_admin_identities_email", table_name="admin_identities")
    op.drop_index("ix_admin_identities_clerk_user_id", table_name="admin_identities")
    op.drop_index("ix_admin_identities_admin_user", table_name="admin_identities")
    op.drop_table("admin_identities")
