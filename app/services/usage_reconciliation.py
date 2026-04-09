from __future__ import annotations

from typing import Any

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.logging import get_logger
from app.models.portal import UsageEvent, UsageReconciliationRun
from app.services.usage_ingestion import UsageIngestionService
from app.utils.time import utc_now

logger = get_logger(__name__)


class UsageReconciliationService:
    def __init__(self, usage_ingestion_service: UsageIngestionService) -> None:
        self.usage_ingestion_service = usage_ingestion_service

    async def reconcile(self, session: AsyncSession, *, provider: str = "all") -> dict[str, Any]:
        run = UsageReconciliationRun(
            status="running",
            provider=provider,
            started_at=utc_now(),
            created_at=utc_now(),
            details_json={},
        )
        session.add(run)
        await session.flush()

        finalized_count = 0
        failed_count = 0

        pending_rows = list(
            (
                await session.execute(
                    select(UsageEvent).where(
                        UsageEvent.pricing_state == "pending",
                        UsageEvent.external_id.is_not(None),
                    )
                )
            ).scalars().all()
        )
        if provider != "all":
            pending_rows = [row for row in pending_rows if row.provider == provider]

        for row in pending_rows:
            try:
                metadata = row.metadata_json or {}
                if row.provider == "twilio":
                    price_raw = metadata.get("price")
                    if isinstance(price_raw, str) and price_raw.strip():
                        cost = abs(float(price_raw))
                        await self.usage_ingestion_service.finalize_by_external_id(
                            session,
                            provider=row.provider,
                            external_id=str(row.external_id),
                            event_type=row.event_type,
                            cost_usd=cost,
                            source_ref="twilio_status_callback",
                        )
                        finalized_count += 1
            except Exception:  # noqa: BLE001
                failed_count += 1

        pending_count = int(
            (
                await session.scalar(
                    select(func.count())
                    .select_from(UsageEvent)
                    .where(UsageEvent.pricing_state == "pending")
                )
            )
            or 0
        )

        run.status = "success"
        run.finished_at = utc_now()
        run.finalized_count = finalized_count
        run.pending_count = pending_count
        run.failed_count = failed_count
        run.details_json = {
            "provider": provider,
            "finalized_count": finalized_count,
            "pending_count": pending_count,
            "failed_count": failed_count,
        }
        await session.flush()
        logger.info("usage_reconciliation_completed", **run.details_json)
        return run.details_json
