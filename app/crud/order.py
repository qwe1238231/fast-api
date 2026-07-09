from datetime import datetime, timezone
from uuid import UUID

from sqlalchemy import select
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
) -> list[Order]:
    """Recent orders for a user, newest first."""
    stmt = (
        select(Order)
        .where(Order.user_id == user_id)
        .order_by(Order.created_at.desc())
        .limit(limit)
    )
    result = await db.execute(stmt)
    return list(result.scalars().all())



async def transition_order_status(
        db: AsyncSession,
        order: Order,
        new_status: OrderStatus,
) -> None:
    """Set status and matching timestamp. Rejects illegal state transitions."""
    if new_status not in _VALID_TRANSITIONS[order.status]:
        raise InvalidOrderTransition(
            order_id=order.id,
            from_status=order.status.value,
            to_status=new_status.value,
        )
    now = datetime.now(timezone.utc)
    order.status = new_status
    if new_status == OrderStatus.PAID:
        order.paid_at = now
    elif new_status == OrderStatus.CONFIRMED:
        order.confirmed_at = now
    elif new_status == OrderStatus.EXPIRED:
        order.expired_at = now
    elif new_status == OrderStatus.CANCELLED:
        order.cancelled_at = now
    await db.flush()