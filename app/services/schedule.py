from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta
from random import random
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


class ScheduleService:
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

        rules = await self.proactive_rules_for_user(
            session,
            user_id=user.id,
            persona_id=persona_id,
            weekday=effective_now.weekday(),
        )
        if rules:
            matched = False
            for rule in rules:
                if rule.start_time and rule.end_time and in_time_range(
                    effective_now.timetz().replace(tzinfo=None),
                    rule.start_time,
                    rule.end_time,
                ):
                    matched = True
                    if rule.probability is not None and random() > float(rule.probability):
                        return SendDecision(False, "window_probability_skip")
                    break
            if not matched:
                return SendDecision(False, "outside_schedule_window")
        elif random() > float(safety["proactive_probability"]):
            return SendDecision(False, "global_probability_skip")
        return SendDecision(True, "ok")
