from __future__ import annotations

import uuid
from datetime import UTC, datetime
from typing import Any

from sqlalchemy import desc, func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.settings import RuntimeSettings
from app.models.enums import SubscriptionStatus
from app.models.portal import Account, BillingEvent, Subscription, UsageEvent
from app.schemas.site import UsageCreditSummary
from app.utils.time import utc_now


class BillingService:
    def __init__(self, settings: RuntimeSettings) -> None:
        self.settings = settings
        self._stripe = None
        if settings.stripe.enabled and settings.stripe.secret_key:
            try:
                import stripe

                stripe.api_key = settings.stripe.secret_key
                self._stripe = stripe
            except Exception:
                self._stripe = None

    @property
    def available(self) -> bool:
        return self._stripe is not None and self.settings.stripe.enabled

    def price_id_for_plan(self, plan_key: str | None) -> str | None:
        normalized = (plan_key or "").strip().lower()
        if normalized == "voice":
            return self.settings.stripe.voice_price_id or self.settings.stripe.default_price_id
        if normalized == "chat":
            return self.settings.stripe.chat_price_id or self.settings.stripe.default_price_id
        return self.settings.stripe.default_price_id

    def plan_key_for_subscription(self, subscription: Subscription | None) -> str | None:
        if subscription is None or not subscription.stripe_price_id:
            return None
        price_id = subscription.stripe_price_id
        if self.settings.stripe.voice_price_id and price_id == self.settings.stripe.voice_price_id:
            return "voice"
        if self.settings.stripe.chat_price_id and price_id == self.settings.stripe.chat_price_id:
            return "chat"
        lowered = price_id.lower()
        if "voice" in lowered or "connect" in lowered:
            return "voice"
        if "chat" in lowered:
            return "chat"
        return None

    async def get_account_subscription(self, session: AsyncSession, *, account_id: uuid.UUID) -> Subscription | None:
        stmt = (
            select(Subscription)
            .where(Subscription.account_id == account_id)
            .order_by(desc(Subscription.created_at))
            .limit(1)
        )
        return (await session.execute(stmt)).scalar_one_or_none()

    async def account_status(self, session: AsyncSession, *, account_id: uuid.UUID) -> SubscriptionStatus:
        subscription = await self.get_account_subscription(session, account_id=account_id)
        if subscription is None:
            return SubscriptionStatus.incomplete
        return subscription.status

    async def usage_credit_summary(
        self,
        session: AsyncSession,
        *,
        account_id: uuid.UUID,
        subscription: Subscription | None,
    ) -> UsageCreditSummary:
        included = 10.0
        if subscription and subscription.stripe_price_id:
            price_key = subscription.stripe_price_id.lower()
            if "voice" in price_key or "connect" in price_key:
                included = 30.0

        finalized_cost = float(
            (
                await session.scalar(
                    select(func.coalesce(func.sum(UsageEvent.cost_usd), 0.0)).where(
                        UsageEvent.account_id == account_id,
                        UsageEvent.pricing_state == "finalized",
                    )
                )
            )
            or 0.0
        )
        pending_cost = float(
            (
                await session.scalar(
                    select(func.coalesce(func.sum(UsageEvent.estimated_cost_usd), 0.0)).where(
                        UsageEvent.account_id == account_id,
                        UsageEvent.pricing_state == "pending",
                    )
                )
            )
            or 0.0
        )
        latest_pending_at = await session.scalar(
            select(func.max(UsageEvent.occurred_at)).where(
                UsageEvent.account_id == account_id,
                UsageEvent.pricing_state == "pending",
            )
        )
        lag_minutes = 0
        if latest_pending_at is not None:
            if latest_pending_at.tzinfo is None:
                latest_pending_at = latest_pending_at.replace(tzinfo=UTC)
            lag_minutes = int(max(0.0, (utc_now() - latest_pending_at).total_seconds() / 60.0))
        used = finalized_cost
        remaining = float(max(0.0, included - used))
        return UsageCreditSummary(
            included_usd=included,
            used_usd=used,
            remaining_usd=remaining,
            pending_cost_usd=round(pending_cost, 4),
            finalized_cost_usd=round(finalized_cost, 4),
            reconciliation_lag_minutes=max(0, lag_minutes),
            overage_note="Additional usage is billed by meter after included credits are consumed.",
        )

    @staticmethod
    def can_access_path(status: SubscriptionStatus, path: str) -> bool:
        exempt_prefixes = {
            "/app/landing",
            "/app/dashboard",
            "/app/billing",
            "/app/initialize",
            "/app/child",
            "/app/timeline",
            "/app/memory",
            "/app/safety",
            "/app/team",
            "/app/onboarding",
            "/app/security",
            "/app/verify",
            "/app/logout",
        }
        if any(path.startswith(prefix) for prefix in exempt_prefixes):
            return True
        if status in {SubscriptionStatus.active, SubscriptionStatus.trialing, SubscriptionStatus.past_due}:
            return True
        return False

    async def create_checkout_session(
        self,
        session: AsyncSession,
        *,
        account: Account,
        customer_email: str | None,
        clerk_org_id: str | None,
        plan_key: str | None,
        success_url: str,
        cancel_url: str,
    ) -> str:
        if not self.available:
            raise RuntimeError("Stripe is not configured")
        price_id = self.price_id_for_plan(plan_key)
        if not price_id:
            raise RuntimeError("No Stripe price configured for the selected plan")

        stripe = self._stripe
        assert stripe is not None
        create_kwargs: dict[str, Any] = {
            "mode": "subscription",
            "success_url": success_url,
            "cancel_url": cancel_url,
            "line_items": [{"price": price_id, "quantity": 1}],
            "metadata": {
                "account_id": str(account.id),
                "clerk_org_id": clerk_org_id or "",
                "selected_plan_key": (plan_key or "").strip(),
            },
            "allow_promotion_codes": True,
        }
        normalized_email = (customer_email or "").strip()
        if normalized_email and "@clerk.local" not in normalized_email.lower():
            create_kwargs["customer_email"] = normalized_email
        checkout = stripe.checkout.Session.create(**create_kwargs)
        checkout_data = _stripe_object_to_dict(checkout)
        session.add(
            BillingEvent(
                account_id=account.id,
                event_type="checkout.session.created",
                payload_json={"id": checkout_data.get("id"), "url": checkout_data.get("url")},
                created_at=utc_now(),
            )
        )
        await session.flush()
        return str(checkout_data.get("url"))

    async def sync_checkout_session(
        self,
        session: AsyncSession,
        *,
        checkout_session_id: str,
    ) -> Subscription | None:
        if not self.available:
            raise RuntimeError("Stripe is not configured")
        stripe = self._stripe
        assert stripe is not None

        checkout = stripe.checkout.Session.retrieve(checkout_session_id)
        checkout_data = _stripe_object_to_dict(checkout)
        metadata = checkout_data.get("metadata", {}) or {}
        account_id = metadata.get("account_id")
        clerk_org_id = str(metadata.get("clerk_org_id") or "").strip() or None
        if not account_id and clerk_org_id:
            account = await session.scalar(select(Account).where(Account.clerk_org_id == clerk_org_id))
            if account is not None:
                account_id = str(account.id)
        if not account_id:
            return None

        session.add(
            BillingEvent(
                account_id=account_id,
                event_type="checkout.session.returned",
                payload_json={"id": checkout_data.get("id"), "object": checkout_data},
                created_at=utc_now(),
            )
        )
        subscription_payload = checkout_data.get("subscription")
        if isinstance(subscription_payload, str) and subscription_payload:
            subscription_payload = stripe.Subscription.retrieve(subscription_payload)
        if not subscription_payload:
            await session.flush()
            return await self.get_account_subscription(session, account_id=uuid.UUID(account_id))

        payload = _stripe_object_to_dict(subscription_payload)
        subscription = await self._upsert_subscription_from_webhook(session, account_id=account_id, payload=payload)
        await session.flush()
        return subscription

    async def handle_webhook(self, session: AsyncSession, *, payload: bytes, sig_header: str | None) -> dict[str, Any]:
        if not self.available:
            raise RuntimeError("Stripe is not configured")
        if not self.settings.stripe.webhook_secret:
            raise RuntimeError("No STRIPE_WEBHOOK_SECRET configured")

        stripe = self._stripe
        assert stripe is not None
        event = stripe.Webhook.construct_event(payload=payload, sig_header=sig_header, secret=self.settings.stripe.webhook_secret)
        event_data = _stripe_object_to_dict(event)

        event_type = str(event_data.get("type"))
        data_object = event_data.get("data", {}).get("object", {})
        metadata = data_object.get("metadata", {})
        account_id = metadata.get("account_id")
        clerk_org_id = str(metadata.get("clerk_org_id") or "").strip() or None

        if not account_id and clerk_org_id:
            account = await session.scalar(select(Account).where(Account.clerk_org_id == clerk_org_id))
            if account is not None:
                account_id = str(account.id)

        if account_id:
            session.add(
                BillingEvent(
                    account_id=account_id,
                    event_type=event_type,
                    payload_json={"id": event_data.get("id"), "object": data_object},
                    created_at=utc_now(),
                )
            )

        if (
            event_type in {"customer.subscription.created", "customer.subscription.updated", "customer.subscription.deleted"}
            and account_id
        ):
            await self._upsert_subscription_from_webhook(session, account_id=account_id, payload=data_object)

        await session.flush()
        return {"received": True, "type": event_type}

    async def _upsert_subscription_from_webhook(
        self,
        session: AsyncSession,
        *,
        account_id: str,
        payload: dict[str, Any],
    ) -> Subscription:
        stripe_sub_id = payload.get("id")
        existing = await session.scalar(
            select(Subscription).where(Subscription.stripe_subscription_id == stripe_sub_id)
        )
        if existing is None:
            existing = Subscription(account_id=uuid.UUID(account_id))
            session.add(existing)

        existing.stripe_subscription_id = stripe_sub_id
        existing.stripe_customer_id = payload.get("customer")
        price = payload.get("items", {}).get("data", [{}])[0].get("price", {})
        existing.stripe_price_id = price.get("id") if isinstance(price, dict) else None

        raw_status = str(payload.get("status") or "incomplete")
        mapped_status = {
            "trialing": SubscriptionStatus.trialing,
            "active": SubscriptionStatus.active,
            "past_due": SubscriptionStatus.past_due,
            "canceled": SubscriptionStatus.canceled,
        }.get(raw_status, SubscriptionStatus.incomplete)
        existing.status = mapped_status
        existing.current_period_end = _to_dt(payload.get("current_period_end"))
        existing.cancel_at = _to_dt(payload.get("cancel_at"))
        existing.trial_ends_at = _to_dt(payload.get("trial_end"))
        await session.flush()
        return existing


def _to_dt(value: Any) -> datetime | None:
    if value in (None, ""):
        return None
    if isinstance(value, (int, float)):
        return datetime.fromtimestamp(value, tz=UTC)
    return None


def _stripe_object_to_dict(value: Any) -> dict[str, Any]:
    if hasattr(value, "to_dict"):
        converted = value.to_dict()
        if isinstance(converted, dict):
            return converted
    if hasattr(value, "to_dict_recursive"):
        converted = value.to_dict_recursive()
        if isinstance(converted, dict):
            return converted
    if hasattr(value, "_data") and isinstance(getattr(value, "_data"), dict):
        return dict(value._data)
    if isinstance(value, dict):
        return value
    return dict(value)
