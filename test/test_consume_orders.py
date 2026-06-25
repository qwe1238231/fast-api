"""Tests for the order-stream consumer (one-shot drain + the long-lived loop)."""
import asyncio
from uuid import uuid4

import pytest
from sqlalchemy import select, func

from app.core.security import get_password_hash
from app.models.user import User
from app.models.order import Order, OrderStatus
from app.services.inventory import reserve_and_enqueue, ORDER_STREAM_KEY
from app.worker import (
    consume_order_intents,
    run_order_consumer_loop,
    ORDER_CONSUMER_GROUP,
)


async def _make_user(db, username="buyer"):
    user = User(username=username, hashed_password=get_password_hash("secret123"))
    db.add(user)
    await db.commit()
    return user


@pytest.mark.asyncio
async def test_consumer_persists_order(db, redis, published_event):
    user = await _make_user(db)
    key = str(uuid4())
    await reserve_and_enqueue(
        redis, event_id=published_event.id, user_id=user.id,
        quantity=2, total_price_cents=3000, idempotency_key=key,
    )
    # group is normally created in worker startup(); create it here for the test
    await redis.xgroup_create(ORDER_STREAM_KEY, ORDER_CONSUMER_GROUP, id="0", mkstream=True)

    await consume_order_intents({"redis_client": redis})

    order = (
        await db.execute(select(Order).where(Order.idempotency_key == key))
    ).scalar_one()
    assert order.user_id == user.id
    assert order.event_id == published_event.id
    assert order.quantity == 2
    assert order.status == OrderStatus.PENDING

    # acked -> nothing left pending for the group
    summary = await redis.xpending(ORDER_STREAM_KEY, ORDER_CONSUMER_GROUP)
    assert summary["pending"] == 0


@pytest.mark.asyncio
async def test_consumer_idempotent_on_duplicate(db, redis, published_event):
    """Two stream entries with the same idempotency_key -> exactly one order, no crash."""
    user = await _make_user(db)
    key = str(uuid4())
    fields = {
        "user_id": str(user.id),
        "event_id": str(published_event.id),
        "quantity": "1",
        "total_price_cents": "1500",
        "idempotency_key": key,
    }
    await redis.xadd(ORDER_STREAM_KEY, fields)
    await redis.xadd(ORDER_STREAM_KEY, fields)   # duplicate redelivery, same key
    await redis.xgroup_create(ORDER_STREAM_KEY, ORDER_CONSUMER_GROUP, id="0", mkstream=True)

    await consume_order_intents({"redis_client": redis})

    count = (
        await db.execute(
            select(func.count()).select_from(Order).where(Order.idempotency_key == key)
        )
    ).scalar_one()
    assert count == 1                                       # duplicate did not create a 2nd row

    summary = await redis.xpending(ORDER_STREAM_KEY, ORDER_CONSUMER_GROUP)
    assert summary["pending"] == 0                          # both entries acked


@pytest.mark.asyncio
async def test_consumer_loop_drains_then_stops(db, redis, published_event):
    """The long-lived loop persists enqueued intents and exits on stop_event."""
    user = await _make_user(db)
    key = str(uuid4())
    await reserve_and_enqueue(
        redis, event_id=published_event.id, user_id=user.id,
        quantity=1, total_price_cents=1500, idempotency_key=key,
    )
    await redis.xgroup_create(ORDER_STREAM_KEY, ORDER_CONSUMER_GROUP, id="0", mkstream=True)

    stop = asyncio.Event()
    task = asyncio.create_task(run_order_consumer_loop(redis, block_ms=50, stop_event=stop))
    await asyncio.sleep(0.3)        # let it drain the entry
    stop.set()
    await asyncio.wait_for(task, timeout=2)   # exits promptly on stop

    order = (
        await db.execute(select(Order).where(Order.idempotency_key == key))
    ).scalar_one()
    assert order.status == OrderStatus.PENDING
