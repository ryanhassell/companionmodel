"""add customer portal tenancy and billing models

Revision ID: 20260407_0003
Revises: 20260407_0002
Create Date: 2026-04-07 00:15:00
"""

from __future__ import annotations

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql


revision = "20260407_0003"
down_revision = "20260407_0002"
branch_labels = None
depends_on = None


def upgrade() -> None:
    household_role = postgresql.ENUM(
        "owner",
        "guardian",
        "caregiver",
        "viewer",
        name="householdrole",
        create_type=False,
    )
    verification_case_status = postgresql.ENUM(
        "pending",
        "approved",
        "limited",
        "rejected",
        name="verificationcasestatus",
        create_type=False,
    )
    subscription_status = postgresql.ENUM(
        "trialing",
        "active",
        "past_due",
        "canceled",
        "incomplete",
        name="subscriptionstatus",
        create_type=False,
    )

    bind = op.get_bind()
    household_role.create(bind, checkfirst=True)
    verification_case_status.create(bind, checkfirst=True)
    subscription_status.create(bind, checkfirst=True)

    op.create_table(
        "accounts",
        sa.Column("id", sa.UUID(), primary_key=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
        sa.Column("name", sa.String(length=120), nullable=False),
        sa.Column("slug", sa.String(length=120), nullable=False),
        sa.Column("status", sa.String(length=32), nullable=False, server_default="active"),
        sa.Column("metadata_json", sa.JSON(), nullable=False, server_default="{}"),
    )
    op.create_index("ix_accounts_slug", "accounts", ["slug"], unique=True)

    op.create_table(
        "customer_users",
        sa.Column("id", sa.UUID(), primary_key=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
        sa.Column("account_id", sa.UUID(), sa.ForeignKey("accounts.id"), nullable=False),
        sa.Column("email", sa.String(length=255), nullable=False),
        sa.Column("phone_number", sa.String(length=32)),
        sa.Column("password_hash", sa.String(length=255), nullable=False),
        sa.Column("display_name", sa.String(length=120)),
        sa.Column("relationship_label", sa.String(length=80)),
        sa.Column("verification_level", sa.String(length=24), nullable=False, server_default="unverified"),
        sa.Column("is_active", sa.Boolean(), nullable=False, server_default=sa.true()),
        sa.Column("email_verified_at", sa.DateTime(timezone=True)),
        sa.Column("phone_verified_at", sa.DateTime(timezone=True)),
        sa.Column("mfa_enabled", sa.Boolean(), nullable=False, server_default=sa.false()),
        sa.Column("last_login_at", sa.DateTime(timezone=True)),
        sa.Column("failed_login_attempts", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("locked_until", sa.DateTime(timezone=True)),
        sa.Column("profile_json", sa.JSON(), nullable=False, server_default="{}"),
    )
    op.create_index("ix_customer_users_account_email", "customer_users", ["account_id", "email"], unique=True)
    op.create_index("ix_customer_users_email", "customer_users", ["email"], unique=True)

    op.create_table(
        "households",
        sa.Column("id", sa.UUID(), primary_key=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
        sa.Column("account_id", sa.UUID(), sa.ForeignKey("accounts.id"), nullable=False),
        sa.Column("name", sa.String(length=120), nullable=False),
        sa.Column("timezone", sa.String(length=64), nullable=False, server_default="America/New_York"),
        sa.Column("is_self_managed", sa.Boolean(), nullable=False, server_default=sa.false()),
        sa.Column("metadata_json", sa.JSON(), nullable=False, server_default="{}"),
    )
    op.create_index("ix_households_account", "households", ["account_id"])

    op.create_table(
        "role_assignments",
        sa.Column("id", sa.UUID(), primary_key=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
        sa.Column("account_id", sa.UUID(), sa.ForeignKey("accounts.id"), nullable=False),
        sa.Column("household_id", sa.UUID(), sa.ForeignKey("households.id"), nullable=False),
        sa.Column("customer_user_id", sa.UUID(), sa.ForeignKey("customer_users.id"), nullable=False),
        sa.Column("role", household_role, nullable=False),
    )
    op.create_index(
        "ix_role_assignments_scope",
        "role_assignments",
        ["account_id", "household_id", "customer_user_id"],
        unique=True,
    )

    op.create_table(
        "child_profiles",
        sa.Column("id", sa.UUID(), primary_key=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
        sa.Column("account_id", sa.UUID(), sa.ForeignKey("accounts.id"), nullable=False),
        sa.Column("household_id", sa.UUID(), sa.ForeignKey("households.id"), nullable=False),
        sa.Column("companion_user_id", sa.UUID(), sa.ForeignKey("users.id")),
        sa.Column("first_name", sa.String(length=120), nullable=False),
        sa.Column("display_name", sa.String(length=120)),
        sa.Column("birth_year", sa.Integer()),
        sa.Column("notes", sa.Text()),
        sa.Column("preferences_json", sa.JSON(), nullable=False, server_default="{}"),
        sa.Column("boundaries_json", sa.JSON(), nullable=False, server_default="{}"),
        sa.Column("routines_json", sa.JSON(), nullable=False, server_default="{}"),
        sa.Column("is_active", sa.Boolean(), nullable=False, server_default=sa.true()),
    )
    op.create_index("ix_child_profiles_household", "child_profiles", ["household_id"])

    op.create_table(
        "verification_cases",
        sa.Column("id", sa.UUID(), primary_key=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
        sa.Column("account_id", sa.UUID(), sa.ForeignKey("accounts.id"), nullable=False),
        sa.Column("customer_user_id", sa.UUID(), sa.ForeignKey("customer_users.id"), nullable=False),
        sa.Column("status", verification_case_status, nullable=False, server_default="pending"),
        sa.Column("risk_score", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("reason_codes", sa.JSON(), nullable=False, server_default="[]"),
        sa.Column("notes", sa.Text()),
        sa.Column("attestation_accepted_at", sa.DateTime(timezone=True)),
        sa.Column("reviewed_by_admin_id", sa.UUID(), sa.ForeignKey("admin_users.id")),
        sa.Column("reviewed_at", sa.DateTime(timezone=True)),
    )
    op.create_index("ix_verification_cases_status", "verification_cases", ["status", "created_at"])
    op.create_index("ix_verification_cases_account", "verification_cases", ["account_id", "created_at"])

    op.create_table(
        "portal_sessions",
        sa.Column("id", sa.UUID(), primary_key=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
        sa.Column("customer_user_id", sa.UUID(), sa.ForeignKey("customer_users.id"), nullable=False),
        sa.Column("session_token_hash", sa.String(length=128), nullable=False),
        sa.Column("csrf_token", sa.String(length=128), nullable=False),
        sa.Column("user_agent", sa.String(length=300)),
        sa.Column("ip_address", sa.String(length=64)),
        sa.Column("trusted_device", sa.Boolean(), nullable=False, server_default=sa.false()),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("revoked_at", sa.DateTime(timezone=True)),
        sa.Column("last_seen_at", sa.DateTime(timezone=True)),
    )
    op.create_index("ix_portal_sessions_customer", "portal_sessions", ["customer_user_id", "revoked_at"])
    op.create_index("ix_portal_sessions_token", "portal_sessions", ["session_token_hash"], unique=True)

    op.create_table(
        "email_verification_tokens",
        sa.Column("id", sa.UUID(), primary_key=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
        sa.Column("customer_user_id", sa.UUID(), sa.ForeignKey("customer_users.id"), nullable=False),
        sa.Column("token_hash", sa.String(length=128), nullable=False),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("consumed_at", sa.DateTime(timezone=True)),
    )
    op.create_index("ix_email_verification_tokens_hash", "email_verification_tokens", ["token_hash"], unique=True)

    op.create_table(
        "phone_otp_challenges",
        sa.Column("id", sa.UUID(), primary_key=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
        sa.Column("customer_user_id", sa.UUID(), sa.ForeignKey("customer_users.id"), nullable=False),
        sa.Column("phone_number", sa.String(length=32), nullable=False),
        sa.Column("code_hash", sa.String(length=128), nullable=False),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("attempts", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("verified_at", sa.DateTime(timezone=True)),
    )
    op.create_index("ix_phone_otp_challenges_customer", "phone_otp_challenges", ["customer_user_id", "created_at"])

    op.create_table(
        "consent_records",
        sa.Column("id", sa.UUID(), primary_key=True),
        sa.Column("account_id", sa.UUID(), sa.ForeignKey("accounts.id"), nullable=False),
        sa.Column("customer_user_id", sa.UUID(), sa.ForeignKey("customer_users.id"), nullable=False),
        sa.Column("policy_type", sa.String(length=40), nullable=False),
        sa.Column("policy_version", sa.String(length=40), nullable=False),
        sa.Column("accepted_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("ip_address", sa.String(length=64)),
        sa.Column("user_agent", sa.String(length=300)),
    )
    op.create_index(
        "ix_consent_records_user_type",
        "consent_records",
        ["customer_user_id", "policy_type", "accepted_at"],
    )

    op.create_table(
        "subscriptions",
        sa.Column("id", sa.UUID(), primary_key=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
        sa.Column("account_id", sa.UUID(), sa.ForeignKey("accounts.id"), nullable=False),
        sa.Column("stripe_customer_id", sa.String(length=120)),
        sa.Column("stripe_subscription_id", sa.String(length=120)),
        sa.Column("stripe_price_id", sa.String(length=120)),
        sa.Column("status", subscription_status, nullable=False, server_default="incomplete"),
        sa.Column("current_period_end", sa.DateTime(timezone=True)),
        sa.Column("cancel_at", sa.DateTime(timezone=True)),
        sa.Column("trial_ends_at", sa.DateTime(timezone=True)),
    )
    op.create_index("ix_subscriptions_account", "subscriptions", ["account_id"])
    op.create_index("ix_subscriptions_stripe_sub", "subscriptions", ["stripe_subscription_id"], unique=True)

    op.create_table(
        "billing_events",
        sa.Column("id", sa.UUID(), primary_key=True),
        sa.Column("account_id", sa.UUID(), sa.ForeignKey("accounts.id"), nullable=False),
        sa.Column("subscription_id", sa.UUID(), sa.ForeignKey("subscriptions.id")),
        sa.Column("event_type", sa.String(length=80), nullable=False),
        sa.Column("payload_json", sa.JSON(), nullable=False, server_default="{}"),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
    )
    op.create_index("ix_billing_events_account_created", "billing_events", ["account_id", "created_at"])


def downgrade() -> None:
    op.drop_index("ix_billing_events_account_created", table_name="billing_events")
    op.drop_table("billing_events")
    op.drop_index("ix_subscriptions_stripe_sub", table_name="subscriptions")
    op.drop_index("ix_subscriptions_account", table_name="subscriptions")
    op.drop_table("subscriptions")
    op.drop_index("ix_consent_records_user_type", table_name="consent_records")
    op.drop_table("consent_records")
    op.drop_index("ix_phone_otp_challenges_customer", table_name="phone_otp_challenges")
    op.drop_table("phone_otp_challenges")
    op.drop_index("ix_email_verification_tokens_hash", table_name="email_verification_tokens")
    op.drop_table("email_verification_tokens")
    op.drop_index("ix_portal_sessions_token", table_name="portal_sessions")
    op.drop_index("ix_portal_sessions_customer", table_name="portal_sessions")
    op.drop_table("portal_sessions")
    op.drop_index("ix_verification_cases_account", table_name="verification_cases")
    op.drop_index("ix_verification_cases_status", table_name="verification_cases")
    op.drop_table("verification_cases")
    op.drop_index("ix_child_profiles_household", table_name="child_profiles")
    op.drop_table("child_profiles")
    op.drop_index("ix_role_assignments_scope", table_name="role_assignments")
    op.drop_table("role_assignments")
    op.drop_index("ix_households_account", table_name="households")
    op.drop_table("households")
    op.drop_index("ix_customer_users_email", table_name="customer_users")
    op.drop_index("ix_customer_users_account_email", table_name="customer_users")
    op.drop_table("customer_users")
    op.drop_index("ix_accounts_slug", table_name="accounts")
    op.drop_table("accounts")

    bind = op.get_bind()
    sa.Enum(name="subscriptionstatus").drop(bind, checkfirst=True)
    sa.Enum(name="verificationcasestatus").drop(bind, checkfirst=True)
    sa.Enum(name="householdrole").drop(bind, checkfirst=True)
