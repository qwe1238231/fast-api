"""Reset load-test state to a clean baseline before a run.

  1. TRUNCATE orders (RESTART IDENTITY) in Postgres.
  2. Reset the Redis inventory for EVENT_ID to TOTAL_SEATS.
  3. Drop the order stream + idempotency claims + released markers, so state from
     a prior run can't leak in. In particular a stale `released:order:{id}` would
     silently swallow a real seat release once RESTART IDENTITY reuses that id.

Run from the repo root:
    PYTHONPATH=. .venv/bin/python loadtest/reset.py
    EVENT_ID=1 TOTAL_SEATS=50000 PYTHONPATH=. .venv/bin/python loadtest/reset.py
"""
from __future__ import annotations

import asyncio
import os

from redis.asyncio import Redis
from sqlalchemy import text

from app.core.config import get_settings
from app.core.redis import create_redis_client
from app.db.session import engine
from app.services.inventory import ORDER_STREAM_KEY
from app.worker import ORDER_CONSUMER_GROUP, ensure_consumer_group

EVENT_ID = int(os.getenv("EVENT_ID", "1"))
TOTAL_SEATS = int(os.getenv("TOTAL_SEATS", "50000"))


async def _scan_delete(redis: Redis, pattern: str) -> int:
    deleted = 0
    async for key in redis.scan_iter(match=pattern, count=1000):
        await redis.delete(key)
        deleted += 1
    return deleted


async def main() -> None:
    async with engine.begin() as conn:
        await conn.execute(text("TRUNCATE orders RESTART IDENTITY CASCADE"))

    redis = create_redis_client(get_settings().REDIS_URL)
    try:
        # Delete + recreate the stream WITH its consumer group. Deleting alone
        # orphans the running order-consumer's group ("order-writer") — it then
        # loops on NOGROUP and stops draining. ensure_consumer_group re-creates
        # the (empty) stream + group at id=0 so the live consumer keeps working.
        await redis.delete(ORDER_STREAM_KEY)
        await ensure_consumer_group(redis, ORDER_STREAM_KEY, ORDER_CONSUMER_GROUP)
        idem = await _scan_delete(redis, "idempotency:*")
        rel = await _scan_delete(redis, "released:*")
        # Unconditional SET (not the NX set_initial_stock) — this is the authoritative reset.
        await redis.set(f"event:{EVENT_ID}:available", TOTAL_SEATS)
    finally:
        await redis.aclose()

    print(
        f"reset: orders truncated; event:{EVENT_ID}:available={TOTAL_SEATS}; "
        f"stream cleared; dropped {idem} idempotency + {rel} released keys"
    )


if __name__ == "__main__":
    asyncio.run(main())
