from __future__ import annotations

import uuid
from dataclasses import dataclass
from datetime import datetime
from typing import Any

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.logging import get_logger
from app.models.communication import Conversation
from app.models.portal import ChildProfile, UsageEvent
from app.utils.time import utc_now

logger = get_logger(__name__)


@dataclass(slots=True)
class UsageRecordInput:
    account_id: uuid.UUID
    user_id: uuid.UUID | None
    conversation_id: uuid.UUID | None
    provider: str
    product_surface: str
    event_type: str
    external_id: str | None
    idempotency_key: str
    quantity: float
    unit: str
    occurred_at: datetime
    cost_usd: float | None = None
    currency: str = "usd"
    estimated_cost_usd: float | None = None
    source_ref: str | None = None
    metadata_json: dict[str, Any] | None = None


class UsageIngestionService:
    async def resolve_account_id_for_user(self, session: AsyncSession, *, user_id: uuid.UUID) -> uuid.UUID | None:
        account_id = await session.scalar(
            select(ChildProfile.account_id)
            .where(ChildProfile.companion_user_id == user_id)
            .limit(1)
        )
        return account_id

    async def resolve_account_id_for_conversation(
        self,
        session: AsyncSession,
        *,
        conversation_id: uuid.UUID | None,
        user_id: uuid.UUID | None,
    ) -> uuid.UUID | None:
        if user_id is not None:
            found = await self.resolve_account_id_for_user(session, user_id=user_id)
            if found is not None:
                return found
        if conversation_id is None:
            return None
        user_id_from_conversation = await session.scalar(
            select(Conversation.user_id)
            .where(Conversation.id == conversation_id)
            .limit(1)
        )
        if user_id_from_conversation is None:
            return None
        return await self.resolve_account_id_for_user(session, user_id=user_id_from_conversation)

    async def record_event(self, session: AsyncSession, payload: UsageRecordInput) -> UsageEvent:
        existing = await session.scalar(
            select(UsageEvent).where(UsageEvent.idempotency_key == payload.idempotency_key)
        )
        if existing is not None:
            return existing

        pricing_state = "finalized" if payload.cost_usd is not None else "pending"
        usage = UsageEvent(
            account_id=payload.account_id,
            user_id=payload.user_id,
            conversation_id=payload.conversation_id,
            provider=payload.provider,
            product_surface=payload.product_surface,
            event_type=payload.event_type,
            external_id=payload.external_id,
            idempotency_key=payload.idempotency_key,
            quantity=float(payload.quantity),
            unit=payload.unit,
            cost_usd=payload.cost_usd,
            currency=payload.currency,
            pricing_state=pricing_state,
            estimated_cost_usd=payload.estimated_cost_usd,
            estimated_vs_final_delta=(
                (payload.cost_usd - payload.estimated_cost_usd)
                if payload.cost_usd is not None and payload.estimated_cost_usd is not None
                else None
            ),
            source_ref=payload.source_ref,
            occurred_at=payload.occurred_at,
            reconciled_at=utc_now() if pricing_state == "finalized" else None,
            metadata_json=payload.metadata_json or {},
        )
        session.add(usage)
        await session.flush()
        logger.info(
            "usage_event_recorded",
            provider=usage.provider,
            event_type=usage.event_type,
            idempotency_key=usage.idempotency_key,
            pricing_state=usage.pricing_state,
        )
        return usage

    async def finalize_by_external_id(
        self,
        session: AsyncSession,
        *,
        provider: str,
        external_id: str,
        event_type: str,
        cost_usd: float,
        source_ref: str | None = None,
        metadata_patch: dict[str, Any] | None = None,
    ) -> int:
        rows = list(
            (
                await session.execute(
                    select(UsageEvent).where(
                        UsageEvent.provider == provider,
                        UsageEvent.external_id == external_id,
                        UsageEvent.event_type == event_type,
                    )
                )
            ).scalars().all()
        )
        updated = 0
        for row in rows:
            row.cost_usd = cost_usd
            row.pricing_state = "finalized"
            row.reconciled_at = utc_now()
            if row.estimated_cost_usd is not None:
                row.estimated_vs_final_delta = cost_usd - row.estimated_cost_usd
            if source_ref:
                row.source_ref = source_ref
            if metadata_patch:
                row.metadata_json = {**(row.metadata_json or {}), **metadata_patch}
            updated += 1
        if updated:
            await session.flush()
        return updated
