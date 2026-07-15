"""Tests for reserve_and_enqueue — the atomic dedup + decrement + enqueue + claim.

These call the service function directly (no HTTP); the published_event fixture
gives an event with total_seats=5 and stock already seeded in Redis.
"""
import asyncio
from uuid import uuid4
import pytest
from app.services.inventory import (
    reserve_and_enqueue, get_available, ORDER_STREAM_KEY, ReserveOutcome,
)


@pytest.mark.asyncio
async def test_reserve_success(redis, published_event):
    key = str(uuid4())

    result = await reserve_and_enqueue(
        redis,
        event_id=published_event.id,
        user_id=1,
        quantity=1,
        total_price_cents=1500,
        idempotency_key=key,
    )

    assert result.outcome == ReserveOutcome.OK
    assert result.stream_id is not None
    assert await get_available(redis, event_id=published_event.id) == 4   # 5 -> 4
    assert await redis.xlen(ORDER_STREAM_KEY) == 1                        # one intent enqueued
    assert await redis.exists(f"idempotency:{key}") == 1                  # claim written


@pytest.mark.asyncio
async def test_reserve_dedup(redis, published_event):
    """Same idempotency_key twice: second is DUP, stock decremented only once."""
    key = str(uuid4())

    first = await reserve_and_enqueue(
        redis, event_id=published_event.id, user_id=1,
        quantity=1, total_price_cents=1500, idempotency_key=key,
    )
    second = await reserve_and_enqueue(
        redis, event_id=published_event.id, user_id=1,
        quantity=1, total_price_cents=1500, idempotency_key=key,
    )

    assert first.outcome == ReserveOutcome.OK
    assert second.outcome == ReserveOutcome.DUP
    assert await get_available(redis, event_id=published_event.id) == 4   # NOT 3 — only one decrement
    assert await redis.xlen(ORDER_STREAM_KEY) == 1                        # no duplicate enqueue


@pytest.mark.asyncio
async def test_reserve_sold_out(redis, published_event):
    """Requesting more than available: SOLD_OUT, nothing touched."""
    result = await reserve_and_enqueue(
        redis, event_id=published_event.id, user_id=1,
        quantity=6, total_price_cents=9000, idempotency_key=str(uuid4()),
    )

    assert result.outcome == ReserveOutcome.SOLD_OUT
    assert result.available == 5
    assert await get_available(redis, event_id=published_event.id) == 5   # unchanged
    assert await redis.xlen(ORDER_STREAM_KEY) == 0                        # nothing enqueued


@pytest.mark.asyncio
async def test_reserve_concurrent_no_oversell(redis, published_event):
    """50 concurrent single-seat reserves on 5 seats: exactly 5 succeed."""
    results = await asyncio.gather(*[
        reserve_and_enqueue(
            redis, event_id=published_event.id, user_id=1,
            quantity=1, total_price_cents=1500, idempotency_key=str(uuid4()),
        )
        for _ in range(50)
    ])

    ok = sum(1 for r in results if r.outcome == ReserveOutcome.OK)
    sold_out = sum(1 for r in results if r.outcome == ReserveOutcome.SOLD_OUT)

    assert ok == 5
    assert sold_out == 45
    assert await get_available(redis, event_id=published_event.id) == 0
    assert await redis.xlen(ORDER_STREAM_KEY) == 5                        # sold count == basket count
