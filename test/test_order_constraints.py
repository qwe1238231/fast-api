"""DB-level integrity for orders: the status ⟺ *_at forward correspondence.

These exercise the CHECK constraints (ck_orders_*_at), not application logic —
they persist rows that bypass transition_order_status() to prove the database
itself rejects a milestone status whose timestamp is missing.
"""
from datetime import datetime, timezone
from uuid import uuid4

import pytest
from sqlalchemy.exc import IntegrityError

from app.models.order import Order, OrderStatus
from app.models.user import User


async def _make_user(db, username: str) -> User:
    user = User(username=username, hashed_password="x")
    db.add(user)
    await db.flush()          # 拿到 user.id,滿足 orders.user_id 外鍵
    return user


@pytest.mark.asyncio
async def test_db_rejects_paid_status_without_paid_at(db, published_event):
    user = await _make_user(db, "ck_reject")

    order = Order(
        user_id=user.id,
        event_id=published_event.id,
        quantity=1,
        total_price_cents=1000,
        idempotency_key=uuid4(),
        status=OrderStatus.PAID,          # paid_at 省略 → NULL,違反 ck_orders_paid_at
    )
    db.add(order)
    with pytest.raises(IntegrityError):
        await db.flush()


@pytest.mark.asyncio
async def test_db_accepts_paid_status_with_paid_at(db, published_event):
    user = await _make_user(db, "ck_accept")

    order = Order(
        user_id=user.id,
        event_id=published_event.id,
        quantity=1,
        total_price_cents=1000,
        idempotency_key=uuid4(),
        status=OrderStatus.PAID,
        paid_at=datetime.now(timezone.utc),   # 有對應時間戳 → 合法
    )
    db.add(order)
    await db.flush()                          # 不該報錯
    assert order.id is not None
