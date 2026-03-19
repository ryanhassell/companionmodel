from __future__ import annotations

from contextlib import asynccontextmanager

from apscheduler.schedulers.asyncio import AsyncIOScheduler

from app.core.logging import get_logger
from app.core.settings import RuntimeSettings
from app.db.session import get_sessionmaker
from app.models.admin import JobRun
from app.models.enums import JobStatus
from app.services.container import ServiceContainer
from app.utils.time import utc_now

logger = get_logger(__name__)


class SchedulerService:
    def __init__(self, settings: RuntimeSettings, container: ServiceContainer) -> None:
        self.settings = settings
        self.container = container
        self.scheduler = AsyncIOScheduler(timezone=settings.app.timezone)

    def start(self) -> None:
        self._register_jobs()
        self.scheduler.start()
        logger.info("scheduler_started")

    def shutdown(self) -> None:
        if self.scheduler.running:
            self.scheduler.shutdown(wait=False)
            logger.info("scheduler_stopped")

    def _register_jobs(self) -> None:
        self.scheduler.add_job(
            self.run_proactive_scan,
            "interval",
            seconds=self.settings.scheduling.proactive_scan_seconds,
            id="proactive_scan",
            replace_existing=True,
            max_instances=1,
        )
        self.scheduler.add_job(
            self.run_memory_consolidation,
            "interval",
            minutes=self.settings.scheduling.memory_consolidation_minutes,
            id="memory_consolidation",
            replace_existing=True,
            max_instances=1,
        )
        self.scheduler.add_job(
            self.run_embed_pending,
            "interval",
            minutes=self.settings.scheduling.embed_pending_minutes,
            id="embed_pending",
            replace_existing=True,
            max_instances=1,
        )

    @asynccontextmanager
    async def _job_context(self, job_name: str):
        sessionmaker = get_sessionmaker()
        async with sessionmaker() as session:
            run = JobRun(job_name=job_name, status=JobStatus.running, started_at=utc_now())
            session.add(run)
            await session.commit()
            try:
                yield session, run
                run.status = JobStatus.success
            except Exception as exc:
                run.status = JobStatus.failed
                run.details_json = {"error": str(exc)}
                raise
            finally:
                run.finished_at = utc_now()
                await session.commit()

    async def run_proactive_scan(self) -> None:
        async with self._job_context("proactive_scan") as (session, run):
            run.details_json = {"sent": await self.container.proactive_service.scan(session)}

    async def run_memory_consolidation(self) -> None:
        async with self._job_context("memory_consolidation") as (session, run):
            config = self.settings.model_dump(mode="json")
            run.details_json = {"created": await self.container.memory_service.consolidate(session, config=config)}

    async def run_embed_pending(self) -> None:
        async with self._job_context("embed_pending") as (session, run):
            config = self.settings.model_dump(mode="json")
            run.details_json = {"embedded": await self.container.memory_service.embed_pending_items(session, config=config)}
