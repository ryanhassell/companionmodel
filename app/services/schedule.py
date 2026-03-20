from __future__ import annotations

import hashlib
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Any

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.communication import MediaAsset, Message
from app.models.configuration import ScheduleRule
from app.models.enums import Direction, ScheduleRuleType
from app.models.user import User
from app.utils.time import in_time_range, local_today, now_in_timezone, parse_clock, utc_now


@dataclass(slots=True)
class SendDecision:
    allowed: bool
    reason: str


@dataclass(slots=True)
class _WindowSpec:
    name: str
    start: Any
    end: Any
    probability_multiplier: float = 1.0


class ScheduleService:
    def _stable_ratio(self, *parts: object) -> float:
        seed = "|".join(str(part) for part in parts).encode("utf-8")
        digest = hashlib.sha256(seed).digest()
        return int.from_bytes(digest[:8], "big") / float(2**64 - 1)

    def _no_contact_factor(self, user: User, *, safety_config: dict[str, Any]) -> float:
        last_contact = max(
            [timestamp for timestamp in [user.last_inbound_at, user.last_outbound_at] if timestamp is not None],
            default=None,
        )
        if last_contact is None:
            return 0.0
        elapsed_hours = max((utc_now() - last_contact).total_seconds() / 3600.0, 0.0)
        min_gap_hours = max(float(safety_config["proactive_min_gap_minutes"]) / 60.0, 0.1)
        max_gap_hours = max(float(safety_config["proactive_max_gap_minutes"]) / 60.0, min_gap_hours + 0.1)
        if elapsed_hours <= min_gap_hours:
            return 0.0
        if elapsed_hours >= max_gap_hours:
            return 1.0
        return (elapsed_hours - min_gap_hours) / (max_gap_hours - min_gap_hours)

    def _chance_from_no_contact_factor(self, factor: float) -> float:
        if factor >= 1.0:
            return 1.0
        scaled = max(0.0, (factor - 0.45) / 0.55)
        return min(0.95, (scaled**1.5) * 0.85 + 0.02)

    def _default_proactive_windows(self, safety_config: dict[str, Any]) -> list[_WindowSpec]:
        return [
            _WindowSpec(
                "morning",
                parse_clock(safety_config["proactive_morning_start"]),
                parse_clock(safety_config["proactive_morning_end"]),
            ),
            _WindowSpec(
                "midday",
                parse_clock(safety_config["proactive_midday_start"]),
                parse_clock(safety_config["proactive_midday_end"]),
            ),
            _WindowSpec(
                "evening",
                parse_clock(safety_config["proactive_evening_start"]),
                parse_clock(safety_config["proactive_evening_end"]),
            ),
        ]

    def _window_target_reached(self, *, user: User, window: _WindowSpec, effective_now: datetime) -> bool:
        current_time = effective_now.timetz().replace(tzinfo=None)
        start_minutes = (window.start.hour * 60) + window.start.minute
        end_minutes = (window.end.hour * 60) + window.end.minute
        window_span = max(end_minutes - start_minutes, 1)
        target_offset = int(self._stable_ratio(user.id, effective_now.date().isoformat(), window.name, "minute") * window_span)
        target_minutes = start_minutes + target_offset
        current_minutes = (current_time.hour * 60) + current_time.minute
        return current_minutes >= target_minutes

    def _window_probability_allows(
        self,
        *,
        user: User,
        effective_now: datetime,
        window: _WindowSpec,
        chance: float,
    ) -> bool:
        if chance >= 1.0:
            return True
        roll = self._stable_ratio(user.id, effective_now.date().isoformat(), window.name, "send")
        return roll <= min(max(chance * window.probability_multiplier, 0.0), 0.999)

    async def outbound_count_today(
        self,
        session: AsyncSession,
        *,
        user_id,
        timezone_name: str,
    ) -> int:
        today = local_today(timezone_name)
        tomorrow = today + timedelta(days=1)
        start = datetime.combine(today, datetime.min.time(), tzinfo=now_in_timezone(timezone_name).tzinfo)
        end = datetime.combine(tomorrow, datetime.min.time(), tzinfo=now_in_timezone(timezone_name).tzinfo)
        stmt = select(func.count()).select_from(Message).where(
            Message.user_id == user_id,
            Message.direction == Direction.outbound,
            Message.created_at >= start,
            Message.created_at < end,
        )
        return int((await session.scalar(stmt)) or 0)

    async def image_count_today(self, session: AsyncSession, *, user_id, timezone_name: str) -> int:
        today = local_today(timezone_name)
        tomorrow = today + timedelta(days=1)
        start = datetime.combine(today, datetime.min.time(), tzinfo=now_in_timezone(timezone_name).tzinfo)
        end = datetime.combine(tomorrow, datetime.min.time(), tzinfo=now_in_timezone(timezone_name).tzinfo)
        stmt = select(func.count()).select_from(MediaAsset).where(
            MediaAsset.user_id == user_id,
            MediaAsset.generation_status == "ready",
            MediaAsset.created_at >= start,
            MediaAsset.created_at < end,
        )
        return int((await session.scalar(stmt)) or 0)

    def is_quiet_hours(self, now: datetime, safety_config: dict[str, Any]) -> bool:
        start = parse_clock(safety_config["quiet_hours_start"])
        end = parse_clock(safety_config["quiet_hours_end"])
        return in_time_range(now.timetz().replace(tzinfo=None), start, end)

    async def can_send_message(
        self,
        session: AsyncSession,
        *,
        user: User,
        config: dict[str, Any],
        now: datetime | None = None,
        ignore_quiet_hours: bool = False,
    ) -> SendDecision:
        effective_now = now or now_in_timezone(user.timezone)
        safety = config["safety"]
        if not user.is_enabled:
            return SendDecision(False, "user_disabled")
        if not ignore_quiet_hours and self.is_quiet_hours(effective_now, safety):
            return SendDecision(False, "quiet_hours")
        daily_count = await self.outbound_count_today(session, user_id=user.id, timezone_name=user.timezone)
        if daily_count >= int(safety["daily_message_cap"]):
            return SendDecision(False, "daily_message_cap")
        if user.last_outbound_at and user.last_outbound_at > utc_now() - timedelta(minutes=int(safety["cooldown_minutes"])):
            return SendDecision(False, "cooldown")
        return SendDecision(True, "ok")

    async def proactive_rules_for_user(
        self,
        session: AsyncSession,
        *,
        user_id,
        persona_id,
        weekday: int,
    ) -> list[ScheduleRule]:
        stmt = select(ScheduleRule).where(
            ScheduleRule.rule_type == ScheduleRuleType.proactive_window,
            ScheduleRule.enabled.is_(True),
            ScheduleRule.weekday == weekday,
        )
        rows = (await session.execute(stmt)).scalars().all()
        return [
            row
            for row in rows
            if (row.user_id is None or row.user_id == user_id)
            and (row.persona_id is None or row.persona_id == persona_id)
        ]

    async def should_send_proactive_message(
        self,
        session: AsyncSession,
        *,
        user: User,
        persona_id,
        config: dict[str, Any],
        now: datetime | None = None,
    ) -> SendDecision:
        effective_now = now or now_in_timezone(user.timezone)
        basic = await self.can_send_message(session, user=user, config=config, now=effective_now)
        if not basic.allowed:
            return basic

        safety = config["safety"]
        if user.last_inbound_at and user.last_inbound_at > utc_now() - timedelta(minutes=int(safety["proactive_min_gap_minutes"])):
            return SendDecision(False, "recent_user_activity")
        if user.last_outbound_at and user.last_outbound_at > utc_now() - timedelta(minutes=int(safety["proactive_min_gap_minutes"])):
            return SendDecision(False, "recent_outbound")

        active_window: _WindowSpec | None = None
        rules = await self.proactive_rules_for_user(
            session,
            user_id=user.id,
            persona_id=persona_id,
            weekday=effective_now.weekday(),
        )
        if rules:
            for rule in sorted(rules, key=lambda item: item.priority):
                if rule.start_time and rule.end_time and in_time_range(
                    effective_now.timetz().replace(tzinfo=None),
                    rule.start_time,
                    rule.end_time,
                ):
                    active_window = _WindowSpec(
                        name=f"rule:{rule.id}",
                        start=rule.start_time,
                        end=rule.end_time,
                        probability_multiplier=float(rule.probability) if rule.probability is not None else 1.0,
                    )
                    break
            if active_window is None:
                return SendDecision(False, "outside_schedule_window")
        else:
            active_window = next(
                (
                    window
                    for window in self._default_proactive_windows(safety)
                    if in_time_range(
                        effective_now.timetz().replace(tzinfo=None),
                        window.start,
                        window.end,
                    )
                ),
                None,
            )
            if active_window is None:
                return SendDecision(False, "outside_default_proactive_windows")

        if not self._window_target_reached(user=user, window=active_window, effective_now=effective_now):
            return SendDecision(False, "before_window_target")

        factor = self._no_contact_factor(user, safety_config=safety)
        if factor <= 0.0:
            return SendDecision(False, "no_contact_factor_too_low")
        chance = self._chance_from_no_contact_factor(factor)
        if factor < 1.0 and not self._window_probability_allows(
            user=user,
            effective_now=effective_now,
            window=active_window,
            chance=chance,
        ):
            return SendDecision(False, "no_contact_probability_skip")
        return SendDecision(True, "ok")
