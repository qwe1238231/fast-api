"""DB-level integrity for orders: the status ⟺ *_at forward correspondence.

These exercise the CHECK constraints (ck_orders_*_at), not application logic —
they persist rows that bypass transition_order_status() to prove the database
itself rejects a milestone status whose timestamp is missing.
"""
from datetime import datetime, timedelta, timezone
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


# ─ backward correspondence:資料庫不該容得下狀態機到不了的列

@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("kwargs", "constraint"),
    [
        # 三個終態互斥 —— 到得了其中一個就到不了另一個
        (
            {"status": OrderStatus.EXPIRED, "expired_at": True, "cancelled_at": True},
            "ck_orders_terminal_exclusive",
        ),
        # CONFIRMED 只能從 PAID 來(_VALID_TRANSITIONS)
        (
            {"status": OrderStatus.CONFIRMED, "confirmed_at": True},
            "ck_orders_confirmed_needs_paid",
        ),
    ],
)
async def test_db_rejects_states_the_machine_cannot_reach(
    db, published_event, kwargs, constraint
):
    """這些列不會讓任何程式碼出錯 —— 它們只會讓對帳把同一筆算兩次,
    而那種錯誤沒有人會在當下發現。"""
    user = await _make_user(db, f"ck_{constraint}")
    now = datetime.now(timezone.utc)
    stamps = {k: now for k, v in kwargs.items() if v is True}
    order = Order(
        user_id=user.id,
        event_id=published_event.id,
        quantity=1,
        total_price_cents=1000,
        idempotency_key=uuid4(),
        status=kwargs["status"],
        **stamps,
    )
    db.add(order)
    with pytest.raises(IntegrityError) as excinfo:
        await db.flush()
    assert constraint in str(excinfo.value)


@pytest.mark.asyncio
async def test_db_rejects_confirmed_before_paid(db, published_event):
    """先確認後付款。時間戳是各自獨立寫入的,順序沒有任何程式碼在看。"""
    user = await _make_user(db, "ck_order")
    now = datetime.now(timezone.utc)
    db.add(Order(
        user_id=user.id, event_id=published_event.id, quantity=1,
        total_price_cents=1000, idempotency_key=uuid4(),
        status=OrderStatus.CONFIRMED,
        paid_at=now, confirmed_at=now - timedelta(minutes=1),
    ))
    with pytest.raises(IntegrityError) as excinfo:
        await db.flush()
    assert "ck_orders_paid_before_confirmed" in str(excinfo.value)


@pytest.mark.asyncio
async def test_the_normal_paid_then_confirmed_path_is_accepted(db, published_event):
    """對照組。三條負向測試都綠,也可能只是因為所有 INSERT 都被擋了。"""
    user = await _make_user(db, "ck_happy")
    now = datetime.now(timezone.utc)
    order = Order(
        user_id=user.id, event_id=published_event.id, quantity=1,
        total_price_cents=1000, idempotency_key=uuid4(),
        status=OrderStatus.CONFIRMED,
        paid_at=now - timedelta(minutes=1), confirmed_at=now,
    )
    db.add(order)
    await db.flush()
    assert order.id is not None
