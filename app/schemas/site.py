from __future__ import annotations

from pydantic import BaseModel, Field


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


class PortalNavSection(BaseModel):
    key: str
    label: str
    items: list[PortalNavItem]


class MemoryGraphNode(BaseModel):
    id: str
    label: str
    kind: str = "memory"
    memory_type: str
    memory_type_label: str
    summary: str
    entity_id: str | None = None
    entity_kind: str | None = None
    facet: str | None = None
    relation_to_child: str | None = None
    pinned: bool = False
    archived: bool = False
    updated_at: str | None = None
    item_count: int = 0
    world_section: str | None = None
    semantic_group: str | None = None
    semantic_label: str | None = None
    semantic_relation: str | None = None
    semantic_path: list[str] = Field(default_factory=list)
    breadcrumb: list["MemoryInspectorBreadcrumb"] = Field(default_factory=list)
    icon_key: str | None = None
    branch_label: str | None = None


class MemoryGraphEdge(BaseModel):
    id: str
    source: str
    target: str
    kind: str
    relationship_type: str | None = None
    label: str | None = None
    cascades: bool = False


class MemoryLinkedMemory(BaseModel):
    id: str
    title: str
    summary: str
    kind: str
    relationship_label: str
    archived: bool = False
    pinned: bool = False


class MemoryEntityView(BaseModel):
    id: str
    display_name: str
    entity_kind: str
    facet: str
    relation_to_child: str | None = None
    role: str = "primary"
    world_section: str | None = None
    semantic_group: str | None = None
    semantic_label: str | None = None
    semantic_relation: str | None = None
    semantic_path: list[str] = Field(default_factory=list)


class MemoryInspectorBreadcrumb(BaseModel):
    id: str
    label: str
    kind: str = "node"


class MemoryRecentChange(BaseModel):
    id: str
    change_type: str
    title: str
    summary: str
    occurred_at: str | None = None
    memory_id: str | None = None
    node_id: str | None = None
    href: str | None = None
    tone: str = "muted"
    source_label: str | None = None


class MemoryInspector(BaseModel):
    id: str
    title: str
    memory_type: str
    memory_type_label: str
    content: str
    summary: str | None = None
    tags: list[str]
    pinned: bool = False
    archived: bool = False
    importance_score: float = 0.0
    updated_at: str | None = None
    world_section: str | None = None
    semantic_group: str | None = None
    semantic_label: str | None = None
    semantic_relation: str | None = None
    semantic_path: list[str] = Field(default_factory=list)
    primary_entity: MemoryEntityView | None = None
    attached_entities: list[MemoryEntityView] = Field(default_factory=list)
    linked_memories: list[MemoryLinkedMemory]
    breadcrumb: list[MemoryInspectorBreadcrumb] = Field(default_factory=list)
    icon_key: str | None = None


class MemoryDeletePreviewEntry(BaseModel):
    id: str
    title: str
    reason: str


class MemoryDeletePreview(BaseModel):
    memory_id: str
    deleted_count: int
    affected: list[MemoryDeletePreviewEntry]


class PortalChatSavedMemoryView(BaseModel):
    id: str | None = None
    title: str
    content: str
    memory_type: str | None = None


class PortalChatActivityView(BaseModel):
    kind: str
    label: str
    detail: str | None = None
    memory_id: str | None = None
    memory_ids: list[str] = Field(default_factory=list)
    count: int | None = None
    href: str | None = None
    details: list[PortalChatSavedMemoryView] = Field(default_factory=list)


class PortalChatThreadView(BaseModel):
    id: str
    title: str
    preview: str | None = None
    created_at: str | None = None
    updated_at: str | None = None
    message_count: int = 0
    href: str
    is_active: bool = False


class PortalChatMessageView(BaseModel):
    id: str
    sender: str
    body: str
    kind: str = "message"
    run_id: str | None = None
    created_at: str | None = None
    memory_saved: bool = False
    memory_saved_label: str | None = None
    memory_saved_details: list[PortalChatSavedMemoryView] = Field(default_factory=list)
    activity_events: list[PortalChatActivityView] = Field(default_factory=list)


class PortalInitializationStep(BaseModel):
    key: str
    label: str
    description: str


class PortalVoiceProfileView(BaseModel):
    key: str
    label: str
    summary: str
    sample_intro: str
    realtime_voice: str
    preview_available: bool = False


class PortalResonaPresetView(BaseModel):
    key: str
    label: str
    default_name: str
    summary: str
    description: str
    voice_profile_key: str
    tone_preview: str
    help_preview: str
    avoid_preview: str
    anchor_preview: str


class PortalResonaSummaryView(BaseModel):
    display_name: str | None = None
    mode: str | None = None
    preset_key: str | None = None
    preset_label: str | None = None
    voice_profile_key: str | None = None
    voice_label: str | None = None
    summary: str | None = None
    preview_available: bool = False
    source_type: str | None = None


class PortalInitializationSummary(BaseModel):
    household_name: str | None = None
    relationship_label: str | None = None
    child_name: str | None = None
    child_phone_number: str | None = None
    resona_name: str | None = None
    resona_voice_label: str | None = None
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
    resona_summary: PortalResonaSummaryView | None = None


class PublicSiteContext(BaseModel):
    brand_name: str
    canonical_domain: str
    support_email: str
    privacy_url: str
    terms_url: str
    safety_policy_url: str


class DashboardUsageMetric(BaseModel):
    label: str
    value: str
    detail: str | None = None
    tone: str = "neutral"


class DashboardUsageHero(BaseModel):
    title: str
    summary: str
    metrics: list[DashboardUsageMetric] = Field(default_factory=list)
    progress_percent: int = 0
    progress_tone: str = "neutral"
    note: str | None = None
    primary_cta_label: str
    primary_cta_href: str


class DashboardStatusItem(BaseModel):
    label: str
    value: str
    detail: str | None = None
    tone: str = "neutral"


class DashboardInsightCard(BaseModel):
    label: str
    title: str
    summary: str
    href: str
    action_label: str
    tone: str = "neutral"


class DashboardConversationPreview(BaseModel):
    direction_label: str
    channel_label: str
    timestamp_label: str
    body: str
    href: str


class DashboardMemoryPreview(BaseModel):
    title: str
    memory_type_label: str
    summary: str
    updated_label: str
    href: str


class DashboardSafetyPreview(BaseModel):
    title: str
    severity_label: str
    timestamp_label: str
    detail: str
    tone: str = "neutral"
    href: str


class DashboardCalloutAction(BaseModel):
    label: str
    href: str
    kind: str = "primary"


class DashboardCallout(BaseModel):
    title: str
    body: str
    tone: str = "warning"
    actions: list[DashboardCalloutAction] = Field(default_factory=list)


class GuidancePlanCard(BaseModel):
    title: str
    summary: str
    steps: list[str] = Field(default_factory=list)
    href: str
    action_label: str
    tone: str = "neutral"


class GuidanceQuestionCard(BaseModel):
    key: str
    question: str
    why_it_matters: str
    suggested_prompt: str
    href: str
    action_label: str
    tone: str = "neutral"


class ParentDashboardContext(BaseModel):
    household_name: str
    child_name: str
    subscription_status: str
    subscription_label: str
    role_label: str
    household_summary: str
    child_status_label: str
    child_status_detail: str
    usage_credit_summary: UsageCreditSummary
    usage_hero: DashboardUsageHero
    status_items: list[DashboardStatusItem] = Field(default_factory=list)
    insights: list[DashboardInsightCard] = Field(default_factory=list)
    conversation_previews: list[DashboardConversationPreview] = Field(default_factory=list)
    memory_previews: list[DashboardMemoryPreview] = Field(default_factory=list)
    safety_previews: list[DashboardSafetyPreview] = Field(default_factory=list)
    callout: DashboardCallout | None = None


class AdminAccessPolicy(BaseModel):
    internal_only: bool
    allowlist_cidrs: list[str]
    trusted_header_name: str
