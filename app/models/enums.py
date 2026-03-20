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
