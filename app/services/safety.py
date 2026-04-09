from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from sqlalchemy.ext.asyncio import AsyncSession

from app.models.communication import Conversation, Message, SafetyEvent
from app.models.enums import SafetySeverity
from app.models.persona import Persona
from app.models.user import User
from app.services.alerting import AlertingService
from app.utils.text import normalize_text


@dataclass(slots=True)
class SafetyResult:
    distress: bool = False
    obsessive: bool = False
    blocked: bool = False
    severity: SafetySeverity = SafetySeverity.low
    reasons: list[str] = field(default_factory=list)
    safe_reply: str | None = None


class SafetyService:
    def __init__(self, alerting_service: AlertingService) -> None:
        self.alerting_service = alerting_service

    async def evaluate_inbound(
        self,
        session: AsyncSession,
        *,
        text: str,
        user: User,
        persona: Persona | None,
        conversation: Conversation,
        message: Message,
        config: dict[str, Any],
        recent_inbound_count: int = 0,
    ) -> SafetyResult:
        normalized = normalize_text(text)
        safety = config["safety"]
        result = SafetyResult()

        for pattern in safety.get("distress_patterns", []):
            if normalize_text(pattern) in normalized:
                result.distress = True
                result.severity = SafetySeverity.critical
                result.reasons.append(f"distress:{pattern}")
                result.safe_reply = (safety.get("distress_fallback") or [None])[0]
                await self._record_event(
                    session,
                    user=user,
                    persona=persona,
                    conversation=conversation,
                    message=message,
                    event_type="distress_detected",
                    severity=SafetySeverity.critical,
                    detector="pattern_match",
                    details={"pattern": pattern, "text": text},
                )
                await self.alerting_service.send_alert(
                    {
                        "event": "distress_detected",
                        "user_id": str(user.id),
                        "phone_number": user.phone_number,
                        "message_id": str(message.id),
                        "pattern": pattern,
                    }
                )
                break

        if recent_inbound_count >= int(safety.get("obsessive_message_threshold", 10)):
            result.obsessive = True
            if not result.safe_reply:
                result.safe_reply = (safety.get("deescalation_templates") or [None])[0]
            result.reasons.append("obsessive_window_threshold")

        return result

    async def validate_outbound(
        self,
        session: AsyncSession,
        *,
        text: str,
        user: User,
        persona: Persona | None,
        conversation: Conversation,
        config: dict[str, Any],
        source_message: Message | None = None,
    ) -> SafetyResult:
        result = self._evaluate_outbound_policy(text=text, config=config)

        if result.blocked:
            await self._record_event(
                session,
                user=user,
                persona=persona,
                conversation=conversation,
                message=source_message,
                event_type="outbound_blocked",
                severity=result.severity,
                detector="policy_validation",
                details={"text": text, "reasons": result.reasons},
            )
            result.safe_reply = "I want to keep our chats warm and safe, so I’m going to say that a different way."
        return result

    def check_outbound(self, *, text: str, config: dict[str, Any]) -> SafetyResult:
        return self._evaluate_outbound_policy(text=text, config=config)

    def _evaluate_outbound_policy(self, *, text: str, config: dict[str, Any]) -> SafetyResult:
        normalized = normalize_text(text)
        safety = config["safety"]
        result = SafetyResult()
        blocked_patterns = [normalize_text(item) for item in safety.get("blocked_patterns", [])]
        prohibited_topics = [normalize_text(item) for item in safety.get("prohibited_topics", [])]
        dependency_patterns = [
            "you are all i need",
            "dont leave me",
            "don't leave me",
            "only need me",
            "i need you more than anyone",
            "youre mine",
            "you're mine",
        ]

        for pattern in blocked_patterns:
            if pattern and pattern in normalized:
                result.blocked = True
                result.severity = SafetySeverity.high
                result.reasons.append(f"blocked_pattern:{pattern}")
        for topic in prohibited_topics:
            if topic and topic in normalized:
                result.blocked = True
                result.severity = SafetySeverity.high
                result.reasons.append(f"prohibited_topic:{topic}")
        for pattern in dependency_patterns:
            if pattern in normalized:
                result.blocked = True
                result.severity = SafetySeverity.high
                result.reasons.append(f"dependency_pattern:{pattern}")
        if "if you leave" in normalized and "i will" in normalized:
            result.blocked = True
            result.severity = SafetySeverity.high
            result.reasons.append("dependency_escalation:conditional_attachment")
        return result

    async def _record_event(
        self,
        session: AsyncSession,
        *,
        user: User,
        persona: Persona | None,
        conversation: Conversation,
        message: Message | None,
        event_type: str,
        severity: SafetySeverity,
        detector: str,
        details: dict[str, Any],
    ) -> SafetyEvent:
        event = SafetyEvent(
            user_id=user.id,
            persona_id=persona.id if persona else None,
            conversation_id=conversation.id,
            message_id=message.id if message else None,
            event_type=event_type,
            severity=severity,
            detector=detector,
            details_json=details,
        )
        session.add(event)
        await session.flush()
        return event
