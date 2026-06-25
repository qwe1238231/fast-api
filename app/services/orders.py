"""Order service — state transition validation + side effects.

CRUD layer (app/crud/order.py) does raw DB ops. This layer enforces business
rules: which transitions are allowed, and what side effects each entails
(e.g. releasing Redis inventory on cancel/expire).
"""
from datetime import datetime, timezone
from redis.asyncio import Redis as RedisClient
from sqlalchemy.ext.asyncio import AsyncSession
from uuid import UUID

from app.core.exceptions import (
    InvalidOrderTransition, EventCancelled, EventNotOnSale, EventNotFound, InsufficientInventory,
)
from app.crud.order import transition_order_status
from app.models.order import Order, OrderStatus
from app.services.inventory import release, reserve_and_enqueue, ReserveOutcome, ReserveResult
from app.models.event import EventStatus
from app.services.event_cache import get_event_meta

_ALLOWED_TRANSITIONS : dict[OrderStatus, set[OrderStatus]] = {
    OrderStatus.PENDING:{
        OrderStatus.PAID,
        OrderStatus.CANCELLED,
        OrderStatus.EXPIRED,
    },
    OrderStatus.PAID: {
        OrderStatus.CANCELLED,
        OrderStatus.CONFIRMED,
    },
    OrderStatus.CONFIRMED: set(),
    OrderStatus.CANCELLED: set(),
    OrderStatus.EXPIRED: set(),
}


def _validate_transition(order: Order, new_status: OrderStatus) -> None:
    if new_status not in _ALLOWED_TRANSITIONS[order.status]:
        raise InvalidOrderTransition(
            order_id=order.id,
            from_status=order.status.value,
            to_status=new_status.value,
        )


async def mark_paid(db: AsyncSession, order: Order) -> None:
    """Transition PENDING ->PAID."""
    _validate_transition(order, OrderStatus.PAID)
    await transition_order_status(db, order, OrderStatus.PAID)


async def mark_confirmed(db: AsyncSession, order: Order) -> None:
    """Transition PAID -> CONFIRMED."""
    _validate_transition(order, OrderStatus.CONFIRMED)
    await transition_order_status(db, order, OrderStatus.CONFIRMED)


async def cancel_order(
        db: AsyncSession,
        redis: RedisClient,
        order: Order
) -> None:
    """Cancel an order, Releases reserved inventory."""
    _validate_transition(order, OrderStatus.CANCELLED)
    await transition_order_status(db, order, OrderStatus.CANCELLED)
    await release(redis, event_id=order.event_id, quantity=order.quantity)


async def expire_order(
        db: AsyncSession,
        redis: RedisClient,
        order: Order,
) -> None:
    """Mark order as expired due to payment timeout. Releases inventory."""
    _validate_transition(order, OrderStatus.EXPIRED)
    await transition_order_status(db, order, OrderStatus.EXPIRED)
    await release(redis, event_id=order.event_id, quantity=order.quantity
)
    

async def submit_order(
        db: AsyncSession,
        redis: RedisClient,
        *,
        user_id: int,
        event_id: int,
        quantity: int,
        idempotency_key: UUID,
) -> ReserveResult:
    """Validate the event, then atomically reserve + enqueue the order intent.

    Does NOT insert the order — that happens later in the worker. The request
    path only validates (cheap, cached reads) and runs the atomic Redis script.
    Raises InsufficientInventory (-> 409) when sold out; returns OK or DUP
    (both mean "accepted, processing") otherwise.
    """
    event = await get_event_meta(redis, db, event_id=event_id)
    if event is None:
        raise EventNotFound(event_id=event_id)
    if event.status == EventStatus.CANCELLED:
        raise EventCancelled(event_id=event_id)
    if event.status != EventStatus.PUBLISHED:
        raise EventNotOnSale(event_id=event_id)

    now = datetime.now(timezone.utc)
    if not (event.sale_starts_at <= now <= event.sale_ends_at):
        raise EventNotOnSale(event_id=event_id)

    result = await reserve_and_enqueue(
        redis,
        event_id=event_id,
        user_id=user_id,
        quantity=quantity,
        total_price_cents=event.price_cents * quantity,
        idempotency_key=str(idempotency_key),
    )
    if result.outcome == ReserveOutcome.SOLD_OUT:
        raise InsufficientInventory(
            event_id=event_id,
            requested=quantity,
            available=result.available,
        )
    return result