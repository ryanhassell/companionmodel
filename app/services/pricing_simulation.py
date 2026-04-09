from __future__ import annotations

import math
from typing import Any

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.portal import PlanSimulationRun, PlanSimulationScenario, UsageEvent
from app.utils.time import utc_now


class PricingSimulationService:
    async def run_real_family_profile(
        self,
        session: AsyncSession,
        *,
        actor_count: int = 100,
        period_days: int = 30,
        chat_price_usd: float = 24.0,
        voice_price_usd: float = 59.0,
    ) -> dict[str, Any]:
        actor_count = max(1, actor_count)
        period_days = max(1, period_days)

        run = PlanSimulationRun(
            profile="real_family_usage",
            actor_count=actor_count,
            period_days=period_days,
            baseline_chat_price_usd=chat_price_usd,
            baseline_voice_price_usd=voice_price_usd,
            created_at=utc_now(),
            details_json={},
        )
        session.add(run)
        await session.flush()

        finalized_cost_total = float(
            (
                await session.scalar(
                    select(func.coalesce(func.sum(UsageEvent.cost_usd), 0.0)).where(UsageEvent.pricing_state == "finalized")
                )
            )
            or 0.0
        )
        finalized_events = int(
            (await session.scalar(select(func.count()).select_from(UsageEvent).where(UsageEvent.pricing_state == "finalized"))) or 0
        )

        cost_per_actor = finalized_cost_total / max(actor_count, 1)
        projected_chat_revenue = chat_price_usd * actor_count
        projected_voice_revenue = voice_price_usd * math.ceil(actor_count * 0.35)
        projected_revenue = projected_chat_revenue + projected_voice_revenue
        projected_cost = cost_per_actor * actor_count
        margin = ((projected_revenue - projected_cost) / projected_revenue) if projected_revenue else 0.0

        band = "safe"
        if margin < 0.35:
            band = "tight"
        if margin < 0.15:
            band = "negative"

        scenario = PlanSimulationScenario(
            simulation_run_id=run.id,
            name="baseline_24_59",
            plan_chat_price_usd=chat_price_usd,
            plan_voice_price_usd=voice_price_usd,
            included_chat_credits_usd=8.0,
            included_voice_credits_usd=28.0,
            projected_revenue_usd=round(projected_revenue, 4),
            projected_cost_usd=round(projected_cost, 4),
            projected_margin_pct=round(margin * 100.0, 4),
            recommendation_band=band,
            created_at=utc_now(),
            details_json={
                "finalized_cost_total": round(finalized_cost_total, 4),
                "finalized_event_count": finalized_events,
                "cost_per_actor": round(cost_per_actor, 4),
                "profile": "real_family_usage",
            },
        )
        session.add(scenario)

        run.details_json = {
            "scenario_count": 1,
            "baseline": {
                "chat_price_usd": chat_price_usd,
                "voice_price_usd": voice_price_usd,
                "recommendation_band": band,
            },
        }
        await session.flush()
        return {
            "run_id": str(run.id),
            "profile": run.profile,
            "scenario": {
                "name": scenario.name,
                "projected_revenue_usd": scenario.projected_revenue_usd,
                "projected_cost_usd": scenario.projected_cost_usd,
                "projected_margin_pct": scenario.projected_margin_pct,
                "recommendation_band": scenario.recommendation_band,
            },
        }
