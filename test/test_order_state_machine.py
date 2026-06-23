from uuid import uuid4

import pytest

from app.models.order import Order, OrderStatus
from app.services.orders import mark_paid, mark_confirmed, cancel_order
from app.services.inventory import set_initial_stock, reserve, get_available
from app.core.exceptions import InvalidOrderTransition


def make_order(status: OrderStatus = OrderStatus.PENDING) -> Order:
    """記憶體裡的 order,不落地(測規則不需要外鍵)。"""
    return Order(
        user_id=1,
        event_id=1,
        quantity=1,
        total_price_cents=1000,
        idempotency_key=uuid4(),
        status=status,
    )


@pytest.mark.asyncio
async def test_happy_path_pending_paid_confirmed(db):
    order = make_order(OrderStatus.PENDING)
    await mark_paid(db, order)
    assert order.status == OrderStatus.PAID
    assert order.paid_at is not None         # 轉換要蓋時間戳
    await mark_confirmed(db, order)
    assert order.status == OrderStatus.CONFIRMED


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
async def test_cancel_releases_inventory(db, redis):
    await set_initial_stock(redis, event_id=1, total_seats=10)

    order = make_order(OrderStatus.PENDING)
    order.quantity = 2
    await reserve(redis, event_id=1, quantity=order.quantity)   # 剩 8

    await cancel_order(db, redis, order)            # 取消 → 釋放 order.quantity(=2)

    assert order.status == OrderStatus.CANCELLED
    assert await get_available(redis, event_id=1) == 10   # 票全還回來了