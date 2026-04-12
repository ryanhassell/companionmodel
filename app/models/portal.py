from __future__ import annotations

import uuid
from datetime import datetime
from typing import TYPE_CHECKING, Any

from sqlalchemy import Boolean, DateTime, Enum, ForeignKey, Index, Integer, JSON, String, Text
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.db.base import Base, TimestampMixin, UUIDPrimaryKeyMixin
from app.models.enums import HouseholdRole, PortalChatMessageKind, PortalChatRunStatus, SubscriptionStatus, VerificationCaseStatus

if TYPE_CHECKING:
    from app.models.admin import AdminUser
    from app.models.user import User


class Account(UUIDPrimaryKeyMixin, TimestampMixin, Base):
    __tablename__ = "accounts"
    __table_args__ = (
        Index("ix_accounts_slug", "slug", unique=True),
        Index("ix_accounts_clerk_org_id", "clerk_org_id", unique=True),
    )

    name: Mapped[str] = mapped_column(String(120), nullable=False)
    slug: Mapped[str] = mapped_column(String(120), nullable=False)
    clerk_org_id: Mapped[str | None] = mapped_column(String(120))
    status: Mapped[str] = mapped_column(String(32), nullable=False, default="active")
    metadata_json: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict, nullable=False)

    users: Mapped[list[CustomerUser]] = relationship("CustomerUser", back_populates="account")
    households: Mapped[list[Household]] = relationship("Household", back_populates="account")
    verification_cases: Mapped[list[VerificationCase]] = relationship("VerificationCase", back_populates="account")
    subscriptions: Mapped[list[Subscription]] = relationship("Subscription", back_populates="account")
    initialization_state: Mapped[AccountInitialization | None] = relationship(
        "AccountInitialization",
        back_populates="account",
        uselist=False,
    )


class CustomerUser(UUIDPrimaryKeyMixin, TimestampMixin, Base):
    __tablename__ = "customer_users"
    __table_args__ = (
        Index("ix_customer_users_account_email", "account_id", "email", unique=True),
        Index("ix_customer_users_email", "email", unique=True),
        Index("ix_customer_users_clerk_user_id", "clerk_user_id", unique=True),
    )

    account_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("accounts.id"), nullable=False)
    email: Mapped[str] = mapped_column(String(255), nullable=False)
    clerk_user_id: Mapped[str | None] = mapped_column(String(120))
    phone_number: Mapped[str | None] = mapped_column(String(32))
    password_hash: Mapped[str] = mapped_column(String(255), nullable=False)
    display_name: Mapped[str | None] = mapped_column(String(120))
    relationship_label: Mapped[str | None] = mapped_column(String(80))
    verification_level: Mapped[str] = mapped_column(String(24), nullable=False, default="unverified")
    is_active: Mapped[bool] = mapped_column(Boolean, default=True, nullable=False)
    email_verified_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    phone_verified_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    mfa_enabled: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)
    last_login_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    last_clerk_auth_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    failed_login_attempts: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    locked_until: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    profile_json: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict, nullable=False)

    account: Mapped[Account] = relationship("Account", back_populates="users")
    role_assignments: Mapped[list[RoleAssignment]] = relationship("RoleAssignment", back_populates="customer_user")
    sessions: Mapped[list[PortalSession]] = relationship("PortalSession", back_populates="customer_user")
    verification_cases: Mapped[list[VerificationCase]] = relationship("VerificationCase", back_populates="customer_user")
    consent_records: Mapped[list[ConsentRecord]] = relationship("ConsentRecord", back_populates="customer_user")
    email_tokens: Mapped[list[EmailVerificationToken]] = relationship("EmailVerificationToken", back_populates="customer_user")
    otp_challenges: Mapped[list[PhoneOtpChallenge]] = relationship("PhoneOtpChallenge", back_populates="customer_user")


class Household(UUIDPrimaryKeyMixin, TimestampMixin, Base):
    __tablename__ = "households"
    __table_args__ = (
        Index("ix_households_account", "account_id"),
    )

    account_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("accounts.id"), nullable=False)
    name: Mapped[str] = mapped_column(String(120), nullable=False)
    timezone: Mapped[str] = mapped_column(String(64), nullable=False, default="America/New_York")
    is_self_managed: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    metadata_json: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict, nullable=False)

    account: Mapped[Account] = relationship("Account", back_populates="households")
    role_assignments: Mapped[list[RoleAssignment]] = relationship("RoleAssignment", back_populates="household")
    child_profiles: Mapped[list[ChildProfile]] = relationship("ChildProfile", back_populates="household")


class ChildProfile(UUIDPrimaryKeyMixin, TimestampMixin, Base):
    __tablename__ = "child_profiles"
    __table_args__ = (
        Index("ix_child_profiles_household", "household_id"),
    )

    account_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("accounts.id"), nullable=False)
    household_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("households.id"), nullable=False)
    companion_user_id: Mapped[uuid.UUID | None] = mapped_column(ForeignKey("users.id"))
    first_name: Mapped[str] = mapped_column(String(120), nullable=False)
    display_name: Mapped[str | None] = mapped_column(String(120))
    birth_year: Mapped[int | None] = mapped_column(Integer)
    notes: Mapped[str | None] = mapped_column(Text())
    preferences_json: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict, nullable=False)
    boundaries_json: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict, nullable=False)
    routines_json: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict, nullable=False)
    is_active: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True)

    household: Mapped[Household] = relationship("Household", back_populates="child_profiles")
    companion_user: Mapped[User | None] = relationship("User")
    parent_chat_threads: Mapped[list[PortalChatThread]] = relationship("PortalChatThread", back_populates="child_profile")


class PortalChatThread(UUIDPrimaryKeyMixin, TimestampMixin, Base):
    __tablename__ = "portal_chat_threads"
    __table_args__ = (
        Index("ix_portal_chat_threads_customer_child", "customer_user_id", "child_profile_id"),
        Index("ix_portal_chat_threads_account_updated", "account_id", "updated_at"),
    )

    account_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("accounts.id"), nullable=False)
    customer_user_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("customer_users.id"), nullable=False)
    child_profile_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("child_profiles.id"), nullable=False)
    status: Mapped[str] = mapped_column(String(32), nullable=False, default="open")
    metadata_json: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict, nullable=False)
    last_parent_message_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    last_assistant_message_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))

    account: Mapped[Account] = relationship("Account")
    customer_user: Mapped[CustomerUser] = relationship("CustomerUser")
    child_profile: Mapped[ChildProfile] = relationship("ChildProfile", back_populates="parent_chat_threads")
    runs: Mapped[list[PortalChatRun]] = relationship(
        "PortalChatRun",
        back_populates="thread",
        cascade="all, delete-orphan",
    )
    messages: Mapped[list[PortalChatMessage]] = relationship(
        "PortalChatMessage",
        back_populates="thread",
        cascade="all, delete-orphan",
    )


class PortalChatRun(UUIDPrimaryKeyMixin, TimestampMixin, Base):
    __tablename__ = "portal_chat_runs"
    __table_args__ = (
        Index("ix_portal_chat_runs_thread_created", "thread_id", "created_at"),
        Index("ix_portal_chat_runs_account_status", "account_id", "status"),
    )

    account_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("accounts.id"), nullable=False)
    customer_user_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("customer_users.id"), nullable=False)
    child_profile_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("child_profiles.id"), nullable=False)
    thread_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("portal_chat_threads.id", ondelete="CASCADE"), nullable=False)
    status: Mapped[PortalChatRunStatus] = mapped_column(
        Enum(PortalChatRunStatus, name="portalchatrunstatus"),
        nullable=False,
        default=PortalChatRunStatus.running,
    )
    metadata_json: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict, nullable=False)
    model_name: Mapped[str | None] = mapped_column(String(120))
    error_code: Mapped[str | None] = mapped_column(String(80))
    started_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    completed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))

    account: Mapped[Account] = relationship("Account")
    customer_user: Mapped[CustomerUser] = relationship("CustomerUser")
    child_profile: Mapped[ChildProfile] = relationship("ChildProfile")
    thread: Mapped[PortalChatThread] = relationship("PortalChatThread", back_populates="runs")
    messages: Mapped[list[PortalChatMessage]] = relationship("PortalChatMessage", back_populates="run")


class PortalChatMessage(UUIDPrimaryKeyMixin, TimestampMixin, Base):
    __tablename__ = "portal_chat_messages"
    __table_args__ = (
        Index("ix_portal_chat_messages_thread_created", "thread_id", "created_at"),
        Index("ix_portal_chat_messages_run_created", "run_id", "created_at"),
    )

    thread_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("portal_chat_threads.id", ondelete="CASCADE"), nullable=False)
    run_id: Mapped[uuid.UUID | None] = mapped_column(ForeignKey("portal_chat_runs.id", ondelete="SET NULL"))
    sender: Mapped[str] = mapped_column(String(20), nullable=False)
    message_kind: Mapped[PortalChatMessageKind] = mapped_column(
        Enum(PortalChatMessageKind, name="portalchatmessagekind"),
        nullable=False,
        default=PortalChatMessageKind.message,
    )
    body: Mapped[str] = mapped_column(Text(), nullable=False)
    metadata_json: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict, nullable=False)
    model_name: Mapped[str | None] = mapped_column(String(120))
    tokens_in: Mapped[int | None] = mapped_column(Integer)
    tokens_out: Mapped[int | None] = mapped_column(Integer)

    thread: Mapped[PortalChatThread] = relationship("PortalChatThread", back_populates="messages")
    run: Mapped[PortalChatRun | None] = relationship("PortalChatRun", back_populates="messages")


class RoleAssignment(UUIDPrimaryKeyMixin, TimestampMixin, Base):
    __tablename__ = "role_assignments"
    __table_args__ = (
        Index("ix_role_assignments_scope", "account_id", "household_id", "customer_user_id", unique=True),
    )

    account_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("accounts.id"), nullable=False)
    household_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("households.id"), nullable=False)
    customer_user_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("customer_users.id"), nullable=False)
    role: Mapped[HouseholdRole] = mapped_column(Enum(HouseholdRole, name="householdrole"), nullable=False)

    household: Mapped[Household] = relationship("Household", back_populates="role_assignments")
    customer_user: Mapped[CustomerUser] = relationship("CustomerUser", back_populates="role_assignments")


class VerificationCase(UUIDPrimaryKeyMixin, TimestampMixin, Base):
    __tablename__ = "verification_cases"
    __table_args__ = (
        Index("ix_verification_cases_status", "status", "created_at"),
        Index("ix_verification_cases_account", "account_id", "created_at"),
    )

    account_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("accounts.id"), nullable=False)
    customer_user_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("customer_users.id"), nullable=False)
    status: Mapped[VerificationCaseStatus] = mapped_column(
        Enum(VerificationCaseStatus, name="verificationcasestatus"),
        nullable=False,
        default=VerificationCaseStatus.pending,
    )
    risk_score: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    reason_codes: Mapped[list[str]] = mapped_column(JSON, nullable=False, default=list)
    notes: Mapped[str | None] = mapped_column(Text())
    attestation_accepted_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    reviewed_by_admin_id: Mapped[uuid.UUID | None] = mapped_column(ForeignKey("admin_users.id"))
    reviewed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))

    account: Mapped[Account] = relationship("Account", back_populates="verification_cases")
    customer_user: Mapped[CustomerUser] = relationship("CustomerUser", back_populates="verification_cases")
    reviewed_by_admin: Mapped[AdminUser | None] = relationship("AdminUser")


class PortalSession(UUIDPrimaryKeyMixin, TimestampMixin, Base):
    __tablename__ = "portal_sessions"
    __table_args__ = (
        Index("ix_portal_sessions_customer", "customer_user_id", "revoked_at"),
        Index("ix_portal_sessions_token", "session_token_hash", unique=True),
    )

    customer_user_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("customer_users.id"), nullable=False)
    session_token_hash: Mapped[str] = mapped_column(String(128), nullable=False)
    csrf_token: Mapped[str] = mapped_column(String(128), nullable=False)
    user_agent: Mapped[str | None] = mapped_column(String(300))
    ip_address: Mapped[str | None] = mapped_column(String(64))
    trusted_device: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    revoked_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    last_seen_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))

    customer_user: Mapped[CustomerUser] = relationship("CustomerUser", back_populates="sessions")


class EmailVerificationToken(UUIDPrimaryKeyMixin, TimestampMixin, Base):
    __tablename__ = "email_verification_tokens"
    __table_args__ = (
        Index("ix_email_verification_tokens_hash", "token_hash", unique=True),
    )

    customer_user_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("customer_users.id"), nullable=False)
    token_hash: Mapped[str] = mapped_column(String(128), nullable=False)
    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    consumed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))

    customer_user: Mapped[CustomerUser] = relationship("CustomerUser", back_populates="email_tokens")


class PhoneOtpChallenge(UUIDPrimaryKeyMixin, TimestampMixin, Base):
    __tablename__ = "phone_otp_challenges"
    __table_args__ = (
        Index("ix_phone_otp_challenges_customer", "customer_user_id", "created_at"),
    )

    customer_user_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("customer_users.id"), nullable=False)
    phone_number: Mapped[str] = mapped_column(String(32), nullable=False)
    code_hash: Mapped[str] = mapped_column(String(128), nullable=False)
    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    attempts: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    verified_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))

    customer_user: Mapped[CustomerUser] = relationship("CustomerUser", back_populates="otp_challenges")


class ConsentRecord(UUIDPrimaryKeyMixin, Base):
    __tablename__ = "consent_records"
    __table_args__ = (
        Index("ix_consent_records_user_type", "customer_user_id", "policy_type", "accepted_at"),
    )

    account_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("accounts.id"), nullable=False)
    customer_user_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("customer_users.id"), nullable=False)
    policy_type: Mapped[str] = mapped_column(String(40), nullable=False)
    policy_version: Mapped[str] = mapped_column(String(40), nullable=False)
    accepted_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    ip_address: Mapped[str | None] = mapped_column(String(64))
    user_agent: Mapped[str | None] = mapped_column(String(300))

    customer_user: Mapped[CustomerUser] = relationship("CustomerUser", back_populates="consent_records")


class Subscription(UUIDPrimaryKeyMixin, TimestampMixin, Base):
    __tablename__ = "subscriptions"
    __table_args__ = (
        Index("ix_subscriptions_account", "account_id"),
        Index("ix_subscriptions_stripe_sub", "stripe_subscription_id", unique=True),
    )

    account_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("accounts.id"), nullable=False)
    stripe_customer_id: Mapped[str | None] = mapped_column(String(120))
    stripe_subscription_id: Mapped[str | None] = mapped_column(String(120))
    stripe_price_id: Mapped[str | None] = mapped_column(String(120))
    status: Mapped[SubscriptionStatus] = mapped_column(
        Enum(SubscriptionStatus, name="subscriptionstatus"),
        nullable=False,
        default=SubscriptionStatus.incomplete,
    )
    current_period_end: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    cancel_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    trial_ends_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))

    account: Mapped[Account] = relationship("Account", back_populates="subscriptions")
    billing_events: Mapped[list[BillingEvent]] = relationship("BillingEvent", back_populates="subscription")


class AccountInitialization(UUIDPrimaryKeyMixin, TimestampMixin, Base):
    __tablename__ = "account_initializations"
    __table_args__ = (
        Index("ix_account_initializations_account", "account_id", unique=True),
        Index("ix_account_initializations_status", "status", "updated_at"),
    )

    account_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("accounts.id"), nullable=False)
    status: Mapped[str] = mapped_column(String(24), nullable=False, default="in_progress")
    current_step: Mapped[str] = mapped_column(String(40), nullable=False, default="welcome")
    completed_steps_json: Mapped[list[str]] = mapped_column(JSON, nullable=False, default=list)
    selected_plan_key: Mapped[str | None] = mapped_column(String(24))
    snapshot_json: Mapped[dict[str, Any]] = mapped_column(JSON, nullable=False, default=dict)
    started_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    completed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))

    account: Mapped[Account] = relationship("Account", back_populates="initialization_state")


class BillingEvent(UUIDPrimaryKeyMixin, Base):
    __tablename__ = "billing_events"
    __table_args__ = (
        Index("ix_billing_events_account_created", "account_id", "created_at"),
    )

    account_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("accounts.id"), nullable=False)
    subscription_id: Mapped[uuid.UUID | None] = mapped_column(ForeignKey("subscriptions.id"))
    event_type: Mapped[str] = mapped_column(String(80), nullable=False)
    payload_json: Mapped[dict[str, Any]] = mapped_column(JSON, nullable=False, default=dict)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)

    subscription: Mapped[Subscription | None] = relationship("Subscription", back_populates="billing_events")


class UsageEvent(UUIDPrimaryKeyMixin, Base):
    __tablename__ = "usage_events"
    __table_args__ = (
        Index("ix_usage_events_account_created", "account_id", "occurred_at"),
        Index("ix_usage_events_provider_event", "provider", "event_type"),
        Index("ix_usage_events_idempotency", "idempotency_key", unique=True),
        Index("ix_usage_events_external", "provider", "external_id"),
    )

    account_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("accounts.id"), nullable=False)
    user_id: Mapped[uuid.UUID | None] = mapped_column(ForeignKey("users.id"))
    conversation_id: Mapped[uuid.UUID | None] = mapped_column(ForeignKey("conversations.id"))
    provider: Mapped[str] = mapped_column(String(40), nullable=False)
    product_surface: Mapped[str] = mapped_column(String(40), nullable=False)
    event_type: Mapped[str] = mapped_column(String(80), nullable=False)
    external_id: Mapped[str | None] = mapped_column(String(120))
    idempotency_key: Mapped[str] = mapped_column(String(180), nullable=False)
    quantity: Mapped[float] = mapped_column(nullable=False, default=0.0)
    unit: Mapped[str] = mapped_column(String(32), nullable=False, default="unit")
    cost_usd: Mapped[float | None] = mapped_column()
    currency: Mapped[str] = mapped_column(String(8), nullable=False, default="usd")
    pricing_state: Mapped[str] = mapped_column(String(20), nullable=False, default="pending")
    estimated_cost_usd: Mapped[float | None] = mapped_column()
    estimated_vs_final_delta: Mapped[float | None] = mapped_column()
    source_ref: Mapped[str | None] = mapped_column(String(255))
    occurred_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    reconciled_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    metadata_json: Mapped[dict[str, Any]] = mapped_column(JSON, nullable=False, default=dict)


class UsageReconciliationRun(UUIDPrimaryKeyMixin, Base):
    __tablename__ = "usage_reconciliation_runs"
    __table_args__ = (
        Index("ix_usage_reconciliation_runs_created", "created_at"),
        Index("ix_usage_reconciliation_runs_status", "status"),
    )

    status: Mapped[str] = mapped_column(String(20), nullable=False, default="running")
    provider: Mapped[str] = mapped_column(String(40), nullable=False, default="all")
    started_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    finished_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    finalized_count: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    pending_count: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    failed_count: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    details_json: Mapped[dict[str, Any]] = mapped_column(JSON, nullable=False, default=dict)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)


class PlanSimulationRun(UUIDPrimaryKeyMixin, Base):
    __tablename__ = "plan_simulation_runs"
    __table_args__ = (
        Index("ix_plan_simulation_runs_created", "created_at"),
    )

    profile: Mapped[str] = mapped_column(String(40), nullable=False, default="real_family_usage")
    actor_count: Mapped[int] = mapped_column(Integer, nullable=False, default=100)
    period_days: Mapped[int] = mapped_column(Integer, nullable=False, default=30)
    baseline_chat_price_usd: Mapped[float] = mapped_column(nullable=False, default=24.0)
    baseline_voice_price_usd: Mapped[float] = mapped_column(nullable=False, default=59.0)
    details_json: Mapped[dict[str, Any]] = mapped_column(JSON, nullable=False, default=dict)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)


class PlanSimulationScenario(UUIDPrimaryKeyMixin, Base):
    __tablename__ = "plan_simulation_scenarios"
    __table_args__ = (
        Index("ix_plan_simulation_scenarios_run", "simulation_run_id"),
    )

    simulation_run_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("plan_simulation_runs.id"), nullable=False)
    name: Mapped[str] = mapped_column(String(80), nullable=False)
    plan_chat_price_usd: Mapped[float] = mapped_column(nullable=False, default=24.0)
    plan_voice_price_usd: Mapped[float] = mapped_column(nullable=False, default=59.0)
    included_chat_credits_usd: Mapped[float] = mapped_column(nullable=False, default=8.0)
    included_voice_credits_usd: Mapped[float] = mapped_column(nullable=False, default=28.0)
    projected_revenue_usd: Mapped[float] = mapped_column(nullable=False, default=0.0)
    projected_cost_usd: Mapped[float] = mapped_column(nullable=False, default=0.0)
    projected_margin_pct: Mapped[float] = mapped_column(nullable=False, default=0.0)
    recommendation_band: Mapped[str] = mapped_column(String(20), nullable=False, default="tight")
    details_json: Mapped[dict[str, Any]] = mapped_column(JSON, nullable=False, default=dict)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)


class AuthIdentityEvent(UUIDPrimaryKeyMixin, Base):
    __tablename__ = "auth_identity_events"
    __table_args__ = (
        Index("ix_auth_identity_events_account_created", "account_id", "created_at"),
        Index("ix_auth_identity_events_user_created", "customer_user_id", "created_at"),
    )

    account_id: Mapped[uuid.UUID | None] = mapped_column(ForeignKey("accounts.id"))
    customer_user_id: Mapped[uuid.UUID | None] = mapped_column(ForeignKey("customer_users.id"))
    event_type: Mapped[str] = mapped_column(String(80), nullable=False)
    details_json: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict, nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
