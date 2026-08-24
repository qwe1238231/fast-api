"""Set up a fresh, queue-OPEN event for waiting-room (Test B) load testing.

Creates + publishes an event whose registration window is open NOW and whose
admission begins REG_SECONDS from now, so a k6 run can (1) storm the join + poll
endpoints during registration, then (2) watch admission meter users in at
QUEUE_ADMISSION_RATE once the window closes.

Uses explicit queue_opens_at / queue_closes_at (the EventCreate API can't set
them, so we build the row directly), which override the sale_starts_at-based
fallback window.

Prints EVENT_ID=<id> on the last line — feed it to queue_flow.js via EVENT_ID=.

Run from the repo root:
    REG_SECONDS=20 TOTAL_SEATS=100000 PYTHONPATH=. .venv/bin/python loadtest/setup_event_b.py
"""
from __future__ import annotations

import asyncio
import os
from datetime import datetime, timedelta, timezone

from app.core.config import get_settings
from app.core.redis import create_redis_client
from app.db.session import AsyncSessionLocal
from app.models.event import Event, EventStatus
from app.services.publish_event import publish_event

REG_SECONDS = int(os.getenv("REG_SECONDS", "20"))
TOTAL_SEATS = int(os.getenv("TOTAL_SEATS", "100000"))


async def main() -> None:
    now = datetime.now(timezone.utc)
    redis = create_redis_client(get_settings().REDIS_URL)
    try:
        async with AsyncSessionLocal() as db:
            event = Event(
                name="LoadTest Queue Event",
                venue="LoadTest Arena",
                starts_at=now + timedelta(days=30),
                ends_at=now + timedelta(days=30, hours=3),
                sale_starts_at=now + timedelta(seconds=REG_SECONDS + 30),
                sale_ends_at=now + timedelta(days=1),
                queue_opens_at=now - timedelta(seconds=30),            # already open
                queue_closes_at=now + timedelta(seconds=REG_SECONDS),  # admission starts here
                total_seats=TOTAL_SEATS,
                price_cents=1500,
                status=EventStatus.DRAFT,
            )
            db.add(event)
            await db.flush()                          # assign event.id
            await publish_event(db, redis, event)     # stock + salt + admit_start + cache
            await db.commit()
            event_id = event.id
    finally:
        await redis.aclose()

    print(f"registration OPEN now; admission starts in {REG_SECONDS}s; stock={TOTAL_SEATS}")
    print(f"EVENT_ID={event_id}")


if __name__ == "__main__":
    asyncio.run(main())
