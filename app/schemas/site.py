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
    included_usd: int
    used_usd: float
    remaining_usd: float
    overage_note: str


class PortalNavItem(BaseModel):
    href: str
    label: str
    key: str


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
