from __future__ import annotations

import asyncio
import time
from dataclasses import dataclass

from app.core.logging import get_logger
from app.core.settings import RuntimeSettings

logger = get_logger(__name__)


@dataclass(slots=True)
class RateLimitDecision:
    allowed: bool
    remaining: int
    retry_after_seconds: int


class RateLimiterService:
    def __init__(self, settings: RuntimeSettings) -> None:
        self.settings = settings
        self._redis = None
        self._memory: dict[str, tuple[int, float]] = {}
        self._lock = asyncio.Lock()

    async def initialize(self) -> None:
        if not self.settings.redis.url:
            return
        try:
            import redis.asyncio as redis

            self._redis = redis.from_url(self.settings.redis.url, decode_responses=True)
            await self._redis.ping()
            logger.info("rate_limiter_redis_connected")
        except Exception as exc:  # noqa: BLE001
            self._redis = None
            logger.warning("rate_limiter_redis_unavailable", error=str(exc))

    async def close(self) -> None:
        if self._redis is not None:
            await self._redis.aclose()

    async def enforce(self, *, key: str, limit: int, window_seconds: int) -> RateLimitDecision:
        scoped_key = f"{self.settings.redis.key_prefix}{key}"
        if self._redis is not None:
            return await self._enforce_redis(scoped_key=scoped_key, limit=limit, window_seconds=window_seconds)
        return await self._enforce_memory(scoped_key=scoped_key, limit=limit, window_seconds=window_seconds)

    async def _enforce_redis(self, *, scoped_key: str, limit: int, window_seconds: int) -> RateLimitDecision:
        assert self._redis is not None
        pipeline = self._redis.pipeline(transaction=True)
        pipeline.incr(scoped_key)
        pipeline.ttl(scoped_key)
        values = await pipeline.execute()
        count = int(values[0] or 0)
        ttl = int(values[1] or -1)
        if ttl < 0:
            await self._redis.expire(scoped_key, window_seconds)
            ttl = window_seconds
        allowed = count <= limit
        remaining = max(0, limit - count)
        retry_after = ttl if not allowed else 0
        return RateLimitDecision(allowed=allowed, remaining=remaining, retry_after_seconds=retry_after)

    async def _enforce_memory(self, *, scoped_key: str, limit: int, window_seconds: int) -> RateLimitDecision:
        now = time.time()
        async with self._lock:
            count, expires_at = self._memory.get(scoped_key, (0, 0.0))
            if expires_at <= now:
                count = 0
                expires_at = now + window_seconds
            count += 1
            self._memory[scoped_key] = (count, expires_at)
            allowed = count <= limit
            remaining = max(0, limit - count)
            retry_after = max(0, int(expires_at - now)) if not allowed else 0
            return RateLimitDecision(allowed=allowed, remaining=remaining, retry_after_seconds=retry_after)
