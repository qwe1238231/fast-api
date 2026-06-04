from datetime import datetime, timezone
from uuid import UUID

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.order import Order, OrderStatus


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
    """Set status and matching timestamp. No transition validation."""
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