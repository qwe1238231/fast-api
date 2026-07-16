from datetime import datetime, timezone
from uuid import UUID

from sqlalchemy import select, tuple_, update
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.exceptions import InvalidOrderTransition
from app.models.order import Order, OrderStatus


# 訂單狀態機:每個狀態能合法轉移到哪些狀態。空集合 = 終態。
_VALID_TRANSITIONS: dict[OrderStatus, set[OrderStatus]] = {
    OrderStatus.PENDING:   {OrderStatus.PAID, OrderStatus.EXPIRED, OrderStatus.CANCELLED},
    OrderStatus.PAID:      {OrderStatus.CONFIRMED},   # 付款後只能確認,不能取消/逾時
    OrderStatus.CONFIRMED: set(),                     # 終態,不能退
    OrderStatus.EXPIRED:   set(),                     # 終態
    OrderStatus.CANCELLED: set(),                     # 終態
}

# 每個目標狀態對應要蓋的時間戳欄位。
_STATUS_TIMESTAMP: dict[OrderStatus, str] = {
    OrderStatus.PAID:      "paid_at",
    OrderStatus.CONFIRMED: "confirmed_at",
    OrderStatus.EXPIRED:   "expired_at",
    OrderStatus.CANCELLED: "cancelled_at",
}


async def create_order(
        db: AsyncSession,
        *,
        user_id: int,
        event_id: int,
        quantity: int,
        total_price_cents: int,
        idempotency_key: UUID,
) -> Order:
    """Insert a new pending order. Caller must commit."""
    order= Order(
        user_id=user_id,
        event_id=event_id,
        quantity=quantity,
        total_price_cents=total_price_cents,
        idempotency_key=idempotency_key,
    )
    db.add(order)
    await db.flush()
    return order


async def get_order_by_id(
        db: AsyncSession,
        order_id: int,
) -> Order | None:
    """Look up an order by its primary key."""
    return await db.get(Order, order_id)


async def get_order_by_idempotency_key(
        db: AsyncSession,
        idempotency_key: UUID,
) -> Order | None:
    """Look up an order by its idempotency_key (UNIQUE, indexed)."""
    stmt = select(Order).where(Order.idempotency_key == idempotency_key)
    return (await db.execute(stmt)).scalar_one_or_none()


async def list_orders_for_user(
        db: AsyncSession,
        user_id: int,
        *,
        limit: int = 50,
        cursor: tuple[datetime, int] | None = None,
) -> list[Order]:
    """Recent orders for a user, newest first.

    Keyset pagination: pass the previous page's last (created_at, id) as `cursor`
    to get the next (older) page. Uses a row-value comparison so it maps to a
    single index range scan on ix_orders_user_created (user_id, created_at, id) —
    constant cost regardless of how deep the page is, and stable under new inserts.
    """
    stmt = select(Order).where(Order.user_id == user_id)
    if cursor is not None:
        # (created_at, id) < (cur_created_at, cur_id) — id breaks created_at ties
        stmt = stmt.where(tuple_(Order.created_at, Order.id) < cursor)
    stmt = stmt.order_by(Order.created_at.desc(), Order.id.desc()).limit(limit)
    result = await db.execute(stmt)
    return list(result.scalars().all())



async def transition_order_status(
        db: AsyncSession,
        order: Order,
        new_status: OrderStatus,
) -> bool:
    """Compare-and-swap the order to `new_status`. Returns True iff applied.

    Two outcomes are distinguished on purpose:
      - ILLEGAL transition (e.g. CONFIRMED->PENDING): a programming bug -> raises
        InvalidOrderTransition.
      - LEGAL transition that LOST a race (the row already moved out of the state
        we read): returns False, so the caller can skip (expire cron) or map it
        to 409 (endpoint) WITHOUT treating a normal race as an error.

    The UPDATE carries `WHERE status = <expected>`, so it only applies while the
    row is STILL in the state we read. A bare `UPDATE ... WHERE id=N` (the old
    code) let a concurrent writer be silently overwritten — e.g. /pay committing
    just as the expire cron fires flips a paid order to EXPIRED and re-releases
    its seat (oversell). We never mutate the ORM `order` attributes by hand (that
    would mark it dirty and flush a SECOND, unpredicated UPDATE — the same race);
    instead we refresh it from the row we just conditionally updated so the caller
    sees the new state.
    """
    expected = order.status
    if new_status not in _VALID_TRANSITIONS[expected]:
        raise InvalidOrderTransition(
            order_id=order.id,
            from_status=expected.value,
            to_status=new_status.value,
        )

    now = datetime.now(timezone.utc)
    ts_attr = _STATUS_TIMESTAMP[new_status]
    result = await db.execute(
        update(Order)
        .where(Order.id == order.id, Order.status == expected)
        .values(status=new_status, **{ts_attr: now})
        .execution_options(synchronize_session=False)
    )
    if result.rowcount != 1:
        return False  # lost the race: the row already left `expected`

    # Reload the identity-map object from the row we just wrote so the caller sees
    # the new status/timestamp. (expire_on_commit=False keeps it valid post-commit.)
    await db.refresh(order)
    return True