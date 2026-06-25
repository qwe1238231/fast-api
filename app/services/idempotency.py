"""Idempotency service — Redis-backed claim of idempotency_key → order_id.

Prevents duplicate orders on client retry. DB UNIQUE constraint on
orders.idempotency_key acts as backup if Redis check fails (rare race).
"""
from redis.asyncio import Redis

DEFAULT_TTL_SECONDS = 86400

# Claim values written into the claim key (set by the atomic reserve script / worker).
CLAIM_PENDING = "PENDING"   # accepted, order row not written yet
CLAIM_FAILED = "FAILED"     # gave up after retries; seat refunded


def _key(idempotency_key: str) -> str:
    """Key for an idempotency claim."""
    return f"idempotency:{idempotency_key}"

async def try_claim(
        redis: Redis,
        *,
        idempotency_key: str,
        order_id: int,
        ttl_seconds: int = DEFAULT_TTL_SECONDS,
) -> bool:
    """Atomically claim an idempotency_key for a given order.
    
    Returns True if newly claimed (this is the first request).
    Returns False if the key was already claimed (duplicate).
    """
    result = await redis.set(
        _key(idempotency_key),
        order_id,
        nx=True,
        ex=ttl_seconds,
    )
    return bool(result)


async def get_claim_state(
        redis: Redis,
        *,
        idempotency_key: str,
) -> str | None:
    """Return the claim value (CLAIM_PENDING / CLAIM_FAILED) or None if no claim.

    Lets the status endpoint tell "still processing" from "gave up" from "never seen".
    """
    return await redis.get(_key(idempotency_key))


async def mark_claim_failed(
        redis: Redis,
        *,
        idempotency_key: str,
        ttl_seconds: int = DEFAULT_TTL_SECONDS,
) -> None:
    """Flip the claim to FAILED so a polling client sees the failure (seat refunded)."""
    await redis.set(_key(idempotency_key), CLAIM_FAILED, ex=ttl_seconds)


async def get_claimed_order_id(
        redis: Redis,
        *,
        idempotency_key: str,
) -> int | None:
    """If this idempotency_key was previously claimed, return the order_id.
    
    Returns None if the key has never been claimed (or expired).
"""
    val = await redis.get(_key(idempotency_key))
    return int(val) if val is not None else None

