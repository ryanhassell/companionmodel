from __future__ import annotations

from pydantic import BaseModel


class MarketingFeatureCard(BaseModel):
    title: str
    summary: str
    icon: str


class SafetyCapabilityCard(BaseModel):
    title: str
    summary: str
    icon: str


class UsageCreditSummary(BaseModel):
    included_usd: float
    used_usd: float
    remaining_usd: float
    pending_cost_usd: float = 0.0
    finalized_cost_usd: float = 0.0
    reconciliation_lag_minutes: int = 0
    overage_note: str


class PortalNavItem(BaseModel):
    href: str
    label: str
    key: str


class PortalInitializationStep(BaseModel):
    key: str
    label: str
    description: str


class PortalInitializationSummary(BaseModel):
    household_name: str | None = None
    relationship_label: str | None = None
    child_name: str | None = None
    child_phone_number: str | None = None
    preferred_pacing: str | None = None
    response_style: str | None = None
    voice_enabled: bool = False
    proactive_check_ins: bool = True
    parent_visibility_mode: str | None = None
    alert_threshold: str | None = None
    quiet_hours: str | None = None
    daily_cadence: str | None = None
    selected_plan_key: str | None = None
    subscription_status: str = "incomplete"


class PortalInitializationContext(BaseModel):
    current_step: str
    step_order: list[str]
    completed_steps: list[str]
    selected_plan_key: str | None = None
    billing_status: str = "incomplete"
    completion_ready: bool = False
    snapshot: dict[str, object]
    summary: PortalInitializationSummary
    steps: list[PortalInitializationStep]


class PublicSiteContext(BaseModel):
    brand_name: str
    canonical_domain: str
    support_email: str
    privacy_url: str
    terms_url: str
    safety_policy_url: str


class ParentDashboardContext(BaseModel):
    household_name: str
    child_name: str
    subscription_status: str
    usage_credit_summary: UsageCreditSummary


class AdminAccessPolicy(BaseModel):
    internal_only: bool
    allowlist_cidrs: list[str]
    trusted_header_name: str
