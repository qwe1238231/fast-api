"""Tiny Redis fixed-window rate limiter (INCR + EXPIRE).

Keyed by the caller (e.g. by user_id), so it works correctly behind a proxy
without depending on the client IP. Fixed-window is simple and good enough here;
it can burst up to ~2x at a window boundary, which is fine for coarse anti-hammer
limits (not for precise quotas).
"""
from redis.asyncio import Redis

from app.core.exceptions import RateLimited


async def enforce_rate_limit(redis: Redis, key: str, *, limit: int, window_seconds: int) -> None:
    """Raise RateLimited if `key` is hit more than `limit` times per window."""
    full_key = f"rl:{key}"
    count = await redis.incr(full_key)
    if count == 1:
        await redis.expire(full_key, window_seconds)
    if count > limit:
        ttl = await redis.ttl(full_key)
        raise RateLimited(retry_after=ttl if ttl and ttl > 0 else window_seconds)
