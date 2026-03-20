from __future__ import annotations

from datetime import timedelta

from app.models.user import User
from app.services.schedule import ScheduleService
from app.utils.time import now_in_timezone, utc_now


class FakeSession:
    async def scalar(self, stmt):
        return 0

    async def execute(self, stmt):
        return _FakeExecuteResult()


class _FakeExecuteResult:
    def scalars(self):
        return self

    def all(self):
        return []


async def test_schedule_respects_quiet_hours(settings):
    service = ScheduleService()
    user = User(phone_number="+15555550102", timezone="America/New_York", is_enabled=True)
    config = settings.model_dump(mode="json")
    now = now_in_timezone("America/New_York").replace(hour=22, minute=0)
    decision = await service.can_send_message(FakeSession(), user=user, config=config, now=now)
    assert decision.allowed is False
    assert decision.reason == "quiet_hours"


async def test_schedule_respects_cooldown(settings):
    service = ScheduleService()
    user = User(phone_number="+15555550103", timezone="America/New_York", is_enabled=True)
    user.last_outbound_at = utc_now()
    config = settings.model_dump(mode="json")
    now = now_in_timezone("America/New_York").replace(hour=12, minute=0)
    decision = await service.can_send_message(FakeSession(), user=user, config=config, now=now, ignore_quiet_hours=True)
    assert decision.allowed is False
    assert decision.reason == "cooldown"


async def test_proactive_schedule_uses_default_daytime_windows(settings):
    service = ScheduleService()
    user = User(phone_number="+15555550104", timezone="America/New_York", is_enabled=True)
    user.last_inbound_at = utc_now() - timedelta(hours=5)
    config = settings.model_dump(mode="json")
    now = now_in_timezone("America/New_York").replace(hour=15, minute=0)
    decision = await service.should_send_proactive_message(
        FakeSession(),
        user=user,
        persona_id=None,
        config=config,
        now=now,
    )
    assert decision.allowed is False
    assert decision.reason == "outside_default_proactive_windows"


async def test_proactive_schedule_allows_when_no_contact_factor_maxes_out(settings, monkeypatch):
    service = ScheduleService()
    user = User(phone_number="+15555550105", timezone="America/New_York", is_enabled=True)
    user.last_inbound_at = utc_now() - timedelta(hours=8)
    config = settings.model_dump(mode="json")
    now = now_in_timezone("America/New_York").replace(hour=12, minute=30)
    monkeypatch.setattr(service, "_window_target_reached", lambda **kwargs: True)
    decision = await service.should_send_proactive_message(
        FakeSession(),
        user=user,
        persona_id=None,
        config=config,
        now=now,
    )
    assert decision.allowed is True
    assert decision.reason == "ok"
