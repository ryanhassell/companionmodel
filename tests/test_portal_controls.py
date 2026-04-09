from __future__ import annotations

import uuid

from app.models.enums import SubscriptionStatus
from app.services.billing import BillingService
from app.services.rate_limiter import RateLimiterService


async def test_rate_limiter_blocks_after_threshold(settings):
    limiter = RateLimiterService(settings)
    key = "test:signup:127.0.0.1"
    allowed = []
    for _ in range(4):
        decision = await limiter.enforce(key=key, limit=3, window_seconds=30)
        allowed.append(decision.allowed)
    assert allowed == [True, True, True, False]


def test_billing_entitlement_rules(settings):
    service = BillingService(settings)
    assert service.can_access_path(SubscriptionStatus.active, "/app/timeline") is True
    assert service.can_access_path(SubscriptionStatus.trialing, "/app/memory") is True
    assert service.can_access_path(SubscriptionStatus.past_due, "/app/team") is True
    assert service.can_access_path(SubscriptionStatus.incomplete, "/app/timeline") is True
    assert service.can_access_path(SubscriptionStatus.incomplete, "/app/dashboard") is True
    assert service.can_access_path(SubscriptionStatus.canceled, "/app/child") is True
    assert service.can_access_path(SubscriptionStatus.canceled, "/app/billing") is True
    assert service.can_access_path(SubscriptionStatus.canceled, "/app/security") is True
    assert service.can_access_path(SubscriptionStatus.incomplete, "/app/internal/paid-action") is False


async def test_account_status_defaults_incomplete(sqlite_session, settings):
    service = BillingService(settings)
    status = await service.account_status(sqlite_session, account_id=uuid.uuid4())
    assert status == SubscriptionStatus.incomplete
