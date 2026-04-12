from __future__ import annotations

from types import SimpleNamespace

from app.jobs.scheduler import SchedulerService


class _FakeMemoryService:
    async def consolidate(self, session, *, config):
        return 0

    async def run_memory_health(self, session, *, config):
        return {"changes_applied": 0, "changes": []}

    async def embed_pending_items(self, session, *, config):
        return 0


class _FakeContainer:
    def __init__(self) -> None:
        self.memory_service = _FakeMemoryService()
        self.proactive_service = SimpleNamespace(scan=None)
        self.daily_life_service = SimpleNamespace(ensure_daily_state=None)
        self.usage_reconciliation_service = SimpleNamespace(reconcile=None)
        self.conversation_service = SimpleNamespace(get_active_persona=None)


def test_scheduler_registers_daily_memory_health_job(settings):
    scheduler = SchedulerService(settings, _FakeContainer())
    scheduler._register_jobs()

    job = scheduler.scheduler.get_job("memory_health")
    assert job is not None
    trigger_repr = str(job.trigger)
    assert str(settings.scheduling.memory_health_hour) in trigger_repr
    assert str(settings.scheduling.memory_health_minute) in trigger_repr
