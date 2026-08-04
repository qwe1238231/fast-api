from uuid import uuid4

import pytest

from app.core.security import get_password_hash
from app.models.user import User
from app.models.order import Order, OrderStatus
from app.services.orders import mark_paid, mark_confirmed, cancel_order, release_order_seat
from app.services.inventory import reserve, get_available
from app.core.exceptions import InvalidOrderTransition


def make_order(status: OrderStatus = OrderStatus.PENDING) -> Order:
    """In-memory order (not persisted). Enough for ILLEGAL-transition tests: the
    state-machine check raises BEFORE the CAS UPDATE ever runs."""
    return Order(
        user_id=1,
        event_id=1,
        quantity=1,
        total_price_cents=1000,
        idempotency_key=uuid4(),
        status=status,
    )


async def _persist_order(db, event, *, quantity: int = 1) -> Order:
    """A real, persisted PENDING order. LEGAL transitions need this because the
    CAS UPDATE in transition_order_status targets a real row (WHERE id=...)."""
    user = User(username=f"buyer-{uuid4().hex[:8]}", hashed_password=get_password_hash("secret123"))
    db.add(user)
    await db.flush()
    order = Order(
        user_id=user.id,
        event_id=event.id,
        quantity=quantity,
        total_price_cents=1000,
        idempotency_key=uuid4(),
        status=OrderStatus.PENDING,
    )
    db.add(order)
    await db.commit()
    return order


# ---- ILLEGAL transitions: raise on the state-machine check, before the UPDATE ----

@pytest.mark.asyncio
async def test_cannot_confirm_without_paying_first(db):
    order = make_order(OrderStatus.PENDING)
    with pytest.raises(InvalidOrderTransition):
        await mark_confirmed(db, order)      # PENDING → CONFIRMED 非法


@pytest.mark.asyncio
async def test_cannot_pay_a_confirmed_order(db):
    order = make_order(OrderStatus.CONFIRMED)
    with pytest.raises(InvalidOrderTransition):
        await mark_paid(db, order)           # 終態出不去


@pytest.mark.asyncio
async def test_cannot_pay_a_cancelled_order(db):
    order = make_order(OrderStatus.CANCELLED)
    with pytest.raises(InvalidOrderTransition):
        await mark_paid(db, order)


@pytest.mark.asyncio
async def test_cannot_cancel_a_paid_order(db):
    order = make_order(OrderStatus.PAID)
    with pytest.raises(InvalidOrderTransition):
        await cancel_order(db, order)        # PAID → CANCELLED 非法(付款後不能取消)


# ---- LEGAL transitions: need a persisted row for the CAS UPDATE to match ----

@pytest.mark.asyncio
async def test_happy_path_pending_paid_confirmed(db, published_event):
    order = await _persist_order(db, published_event)
    assert await mark_paid(db, order) is True
    assert order.status == OrderStatus.PAID
    assert order.paid_at is not None         # 轉換要蓋時間戳
    assert await mark_confirmed(db, order) is True
    assert order.status == OrderStatus.CONFIRMED


@pytest.mark.asyncio
async def test_cancel_releases_inventory(db, redis, published_event):
    # published_event already set stock = total_seats (5) via set_initial_stock.
    order = await _persist_order(db, published_event, quantity=2)
    await reserve(redis, event_id=published_event.id, quantity=order.quantity)   # 5 -> 3
    assert await get_available(redis, event_id=published_event.id) == 3

    assert await cancel_order(db, order) is True          # PENDING → CANCELLED (transition only)
    await db.commit()
    assert await release_order_seat(db, redis, order) is True  # post-commit seat return

    assert order.status == OrderStatus.CANCELLED
    assert await get_available(redis, event_id=published_event.id) == 5   # 票全還回來了
