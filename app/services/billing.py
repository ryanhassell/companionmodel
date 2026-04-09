from __future__ import annotations

import uuid
from datetime import UTC, datetime
from typing import Any

from sqlalchemy import desc, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.settings import RuntimeSettings
from app.models.enums import SubscriptionStatus
from app.models.portal import Account, BillingEvent, Subscription
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

    def usage_credit_summary(self, subscription: Subscription | None) -> UsageCreditSummary:
        included = 10
        if subscription and subscription.stripe_price_id:
            price_key = subscription.stripe_price_id.lower()
            if "voice" in price_key or "connect" in price_key:
                included = 30
        used = 0.0
        remaining = float(max(0, included) - used)
        return UsageCreditSummary(
            included_usd=included,
            used_usd=used,
            remaining_usd=remaining,
            overage_note="Additional usage is billed by meter after included credits are consumed.",
        )

    @staticmethod
    def can_access_path(status: SubscriptionStatus, path: str) -> bool:
        exempt_prefixes = {
            "/app/billing",
            "/app/security",
            "/app/verify",
            "/app/onboarding",
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
        customer_email: str,
        clerk_org_id: str | None,
        success_url: str,
        cancel_url: str,
    ) -> str:
        if not self.available:
            raise RuntimeError("Stripe is not configured")
        if not self.settings.stripe.default_price_id:
            raise RuntimeError("No STRIPE_DEFAULT_PRICE_ID configured")

        stripe = self._stripe
        assert stripe is not None
        checkout = stripe.checkout.Session.create(
            mode="subscription",
            customer_email=customer_email,
            success_url=success_url,
            cancel_url=cancel_url,
            line_items=[{"price": self.settings.stripe.default_price_id, "quantity": 1}],
            metadata={
                "account_id": str(account.id),
                "clerk_org_id": clerk_org_id or "",
            },
            allow_promotion_codes=True,
        )
        session.add(
            BillingEvent(
                account_id=account.id,
                event_type="checkout.session.created",
                payload_json={"id": checkout.get("id"), "url": checkout.get("url")},
                created_at=utc_now(),
            )
        )
        await session.flush()
        return str(checkout.get("url"))

    async def handle_webhook(self, session: AsyncSession, *, payload: bytes, sig_header: str | None) -> dict[str, Any]:
        if not self.available:
            raise RuntimeError("Stripe is not configured")
        if not self.settings.stripe.webhook_secret:
            raise RuntimeError("No STRIPE_WEBHOOK_SECRET configured")

        stripe = self._stripe
        assert stripe is not None
        event = stripe.Webhook.construct_event(payload=payload, sig_header=sig_header, secret=self.settings.stripe.webhook_secret)

        event_type = str(event.get("type"))
        data_object = event.get("data", {}).get("object", {})
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
                    payload_json={"id": event.get("id"), "object": data_object},
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
