"""Tests for the safety net: reclaim of stuck intents + dead-letter of poison ones."""
from uuid import uuid4

import pytest
from sqlalchemy import select, func

from app.core.security import get_password_hash
from app.models.user import User
from app.models.order import Order, OrderStatus
from app.services.inventory import reserve_and_enqueue, get_available, ORDER_STREAM_KEY
from app.services.idempotency import get_claim_state
from app.worker import (
    consume_order_intents,
    reclaim_stale_order_intents,
    ORDER_CONSUMER_GROUP,
    ORDER_DEAD_LETTER_KEY,
)


async def _make_user(db, username="buyer"):
    user = User(username=username, hashed_password=get_password_hash("secret123"))
    db.add(user)
    await db.commit()
    return user


@pytest.mark.asyncio
async def test_reclaim_recovers_stuck_intent(db, redis, published_event):
    """A consumer read an intent but crashed before acking -> reclaim persists it."""
    user = await _make_user(db)
    key = str(uuid4())
    await reserve_and_enqueue(
        redis, event_id=published_event.id, user_id=user.id,
        quantity=1, total_price_cents=1500, idempotency_key=key,
    )
    await redis.xgroup_create(ORDER_STREAM_KEY, ORDER_CONSUMER_GROUP, id="0", mkstream=True)

    # simulate a crashed consumer: it reads the entry into its PEL but never acks
    await redis.xreadgroup(
        groupname=ORDER_CONSUMER_GROUP, consumername="dead-worker",
        streams={ORDER_STREAM_KEY: ">"}, count=10,
    )
    count = (await db.execute(
        select(func.count()).select_from(Order).where(Order.idempotency_key == key)
    )).scalar_one()
    assert count == 0                                          # nothing persisted yet

    await reclaim_stale_order_intents({"redis_client": redis}, min_idle_ms=0, max_deliveries=5)

    order = (
        await db.execute(select(Order).where(Order.idempotency_key == key))
    ).scalar_one()
    assert order.status == OrderStatus.PENDING                 # recovered
    summary = await redis.xpending(ORDER_STREAM_KEY, ORDER_CONSUMER_GROUP)
    assert summary["pending"] == 0                             # acked


@pytest.mark.asyncio
async def test_poison_intent_dead_lettered(db, redis, published_event):
    """An intent that can never be inserted (bad FK) is dead-lettered, seat refunded."""
    key = str(uuid4())
    # user_id 999999 doesn't exist -> the worker's INSERT hits a FK violation
    await reserve_and_enqueue(
        redis, event_id=published_event.id, user_id=999999,
        quantity=1, total_price_cents=1500, idempotency_key=key,
    )
    assert await get_available(redis, event_id=published_event.id) == 4    # seat taken
    await redis.xgroup_create(ORDER_STREAM_KEY, ORDER_CONSUMER_GROUP, id="0", mkstream=True)

    await consume_order_intents({"redis_client": redis})                   # FK fails -> not acked
    assert await get_available(redis, event_id=published_event.id) == 4    # still taken

    # one delivery already happened; max_deliveries=1 -> give up on this pass
    await reclaim_stale_order_intents({"redis_client": redis}, min_idle_ms=0, max_deliveries=1)

    assert await get_available(redis, event_id=published_event.id) == 5    # seat refunded
    assert await get_claim_state(redis, idempotency_key=key) == "FAILED"   # client will see failed
    assert await redis.xlen(ORDER_DEAD_LETTER_KEY) == 1                    # parked for humans

    summary = await redis.xpending(ORDER_STREAM_KEY, ORDER_CONSUMER_GROUP)
    assert summary["pending"] == 0                                         # removed from PEL
