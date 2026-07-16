"""Regression tests for the consistency audit fixes (batch 1).

Each test reproduces a concrete oversell/lost-update scenario the audit found
and asserts the fix holds. These are the tests that did NOT exist before.
"""
from uuid import uuid4, UUID

import pytest

from app.core.security import get_password_hash
from app.db.session import AsyncSessionLocal
from app.models.user import User
from app.models.order import Order, OrderStatus
from app.services.orders import expire_order, cancel_order, mark_paid, release_order_seat
from app.services.inventory import reserve, get_available
from app.worker import _dead_letter_intent


async def _make_user(db) -> User:
    user = User(username=f"buyer-{uuid4().hex[:8]}", hashed_password=get_password_hash("secret123"))
    db.add(user)
    await db.flush()
    return user


async def _persist_pending_order(db, event, *, quantity: int = 1, idempotency_key: UUID | None = None) -> Order:
    user = await _make_user(db)
    order = Order(
        user_id=user.id, event_id=event.id, quantity=quantity,
        total_price_cents=1500, idempotency_key=idempotency_key or uuid4(),
        status=OrderStatus.PENDING,
    )
    db.add(order)
    await db.commit()
    return order


@pytest.mark.asyncio
async def test_expire_wins_pay_loses_no_overwrite(db, redis, published_event):
    """Finding #1 (worst): the expire cron and /pay both read the order as PENDING;
    whoever commits second must NOT blindly overwrite the first. Here expire commits
    first, so /pay's CAS must miss — the order stays EXPIRED, never flipped to PAID."""
    order = await _persist_pending_order(db, published_event)

    # two independent PENDING snapshots (the cron and /pay each loaded it)
    async with AsyncSessionLocal() as s_cron, AsyncSessionLocal() as s_pay:
        o_cron = await s_cron.get(Order, order.id)
        o_pay = await s_pay.get(Order, order.id)

        assert await expire_order(s_cron, o_cron) is True    # cron wins
        await s_cron.commit()

        assert await mark_paid(s_pay, o_pay) is False        # /pay's stale CAS misses

    await db.refresh(order)
    assert order.status == OrderStatus.EXPIRED               # never overwritten to PAID


@pytest.mark.asyncio
async def test_concurrent_terminal_transitions_only_one_applies(db, redis, published_event):
    """Finding #3: cancel-vs-expire (or double-cancel) on the same PENDING order —
    only ONE CAS applies; the loser returns False and does not also release."""
    order = await _persist_pending_order(db, published_event)

    async with AsyncSessionLocal() as s1, AsyncSessionLocal() as s2:
        o1 = await s1.get(Order, order.id)
        o2 = await s2.get(Order, order.id)

        assert await cancel_order(s1, o1) is True            # cancel wins
        await s1.commit()

        assert await expire_order(s2, o2) is False           # expire lost the race

    await db.refresh(order)
    assert order.status == OrderStatus.CANCELLED


@pytest.mark.asyncio
async def test_release_order_seat_is_idempotent(db, redis, published_event):
    """The SETNX marker makes a replayed release (double cancel / crash-retry) a
    no-op: the seat is returned at most once."""
    order = await _persist_pending_order(db, published_event, quantity=2)
    await reserve(redis, event_id=published_event.id, quantity=2)   # 5 -> 3

    assert await release_order_seat(redis, order) is True           # 3 -> 5
    assert await get_available(redis, event_id=published_event.id) == 5
    assert await release_order_seat(redis, order) is False          # replay = no-op
    assert await get_available(redis, event_id=published_event.id) == 5


@pytest.mark.asyncio
async def test_dead_letter_skips_refund_when_order_persisted(db, redis, published_event):
    """Finding #4: a committed order legitimately holds its seat. If its intent gets
    dead-lettered (commit-then-crash-before-ack, deliveries exhausted), the dead-letter
    must NOT refund — else the seat is double-available (oversell)."""
    key = uuid4()
    order = await _persist_pending_order(db, published_event, idempotency_key=key)
    await reserve(redis, event_id=published_event.id, quantity=1)   # 5 -> 4
    before = await get_available(redis, event_id=published_event.id)

    fields = {
        "user_id": str(order.user_id), "event_id": str(published_event.id),
        "quantity": "1", "total_price_cents": "1500", "idempotency_key": str(key),
    }
    await _dead_letter_intent(redis, "1-0", fields)

    # order row exists -> no refund; seat count unchanged (would be 5 if it wrongly refunded)
    assert await get_available(redis, event_id=published_event.id) == before
