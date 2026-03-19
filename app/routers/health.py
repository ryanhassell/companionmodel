from __future__ import annotations

from fastapi import APIRouter, Depends, Request
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from app.admin.dependencies import get_container
from app.db.session import get_db_session
from app.services.container import ServiceContainer

router = APIRouter(tags=["health"])


@router.get("/health/live")
async def live() -> dict[str, str]:
    return {"status": "ok"}


@router.get("/health/ready")
async def ready(
    session: AsyncSession = Depends(get_db_session),
    container: ServiceContainer = Depends(get_container),
) -> dict[str, object]:
    await session.execute(text("SELECT 1"))
    scheduler = getattr(container, "scheduler_service", None)
    return {
        "status": "ok",
        "database": "ok",
        "scheduler_running": bool(scheduler.scheduler.running if scheduler else False),
    }
