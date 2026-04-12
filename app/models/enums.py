from __future__ import annotations

from enum import StrEnum


class Direction(StrEnum):
    inbound = "inbound"
    outbound = "outbound"


class Channel(StrEnum):
    sms = "sms"
    mms = "mms"
    voice = "voice"
    system = "system"


class MessageStatus(StrEnum):
    queued = "queued"
    processing = "processing"
    sent = "sent"
    delivered = "delivered"
    failed = "failed"
    received = "received"
    blocked = "blocked"


class MediaRole(StrEnum):
    inbound = "inbound"
    outbound = "outbound"
    generated = "generated"


class MemoryType(StrEnum):
    fact = "fact"
    episode = "episode"
    summary = "summary"
    preference = "preference"
    follow_up = "follow_up"
    operator_note = "operator_note"
    safety = "safety"


class MemoryRelationshipType(StrEnum):
    manual_child = "manual_child"
    consolidated_into = "consolidated_into"
    supersedes = "supersedes"


class MemoryEntityKind(StrEnum):
    child = "child"
    family_member = "family_member"
    friend = "friend"
    pet = "pet"
    artist = "artist"
    activity = "activity"
    routine_anchor = "routine_anchor"
    event = "event"
    health_context = "health_context"
    topic = "topic"


class MemoryFacet(StrEnum):
    identity = "identity"
    family = "family"
    friends = "friends"
    pets = "pets"
    interests = "interests"
    favorites = "favorites"
    routines = "routines"
    milestones = "milestones"
    health_context = "health_context"
    preferences = "preferences"
    events = "events"


class EntityRelationKind(StrEnum):
    child_world = "child_world"
    family_member = "family_member"
    friend = "friend"
    pet = "pet"
    favorite = "favorite"
    interest = "interest"
    routine = "routine"
    related = "related"


class PortalChatRunStatus(StrEnum):
    running = "running"
    completed = "completed"
    failed = "failed"


class PortalChatMessageKind(StrEnum):
    message = "message"
    activity = "activity"


class SafetySeverity(StrEnum):
    low = "low"
    medium = "medium"
    high = "high"
    critical = "critical"


class ScheduleRuleType(StrEnum):
    proactive_window = "proactive_window"
    quiet_hours = "quiet_hours"
    follow_up = "follow_up"
    call_window = "call_window"


class DeliveryStatus(StrEnum):
    pending = "pending"
    sent = "sent"
    failed = "failed"
    acknowledged = "acknowledged"


class CallDirection(StrEnum):
    outbound = "outbound"
    inbound = "inbound"


class CallStatus(StrEnum):
    queued = "queued"
    ringing = "ringing"
    in_progress = "in_progress"
    completed = "completed"
    failed = "failed"
    no_answer = "no_answer"


class AppSettingScope(StrEnum):
    global_ = "global"
    persona = "persona"
    user = "user"


class JobStatus(StrEnum):
    idle = "idle"
    running = "running"
    success = "success"
    failed = "failed"


class HouseholdRole(StrEnum):
    owner = "owner"
    guardian = "guardian"
    caregiver = "caregiver"
    viewer = "viewer"


class VerificationCaseStatus(StrEnum):
    pending = "pending"
    approved = "approved"
    limited = "limited"
    rejected = "rejected"


class SubscriptionStatus(StrEnum):
    trialing = "trialing"
    active = "active"
    past_due = "past_due"
    canceled = "canceled"
    incomplete = "incomplete"
