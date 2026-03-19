from __future__ import annotations

from datetime import datetime
from typing import Any

from sqlalchemy.ext.asyncio import AsyncSession

from app.models.admin import AuditEvent
from app.utils.time import utc_now


class AuditService:
    async def record(
        self,
        session: AsyncSession,
        *,
        action: str,
        entity_type: str,
        summary: str,
        admin_user_id: str | None = None,
        entity_id: str | None = None,
        details: dict[str, Any] | None = None,
    ) -> AuditEvent:
        event = AuditEvent(
            admin_user_id=admin_user_id,
            action=action,
            entity_type=entity_type,
            entity_id=entity_id,
            summary=summary,
            details_json=details or {},
            created_at=utc_now(),
        )
        session.add(event)
        await session.flush()
        return event
