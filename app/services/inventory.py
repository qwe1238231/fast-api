"""Inventory service — Redis-backed atomic seat counter.

Single source of truth for "how many seats remain". DB stores `events.total_seats`
as configuration; this module manages live decrement during sale.
"""
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy import  select, func

from redis.asyncio import Redis

from app.core.exceptions import InsufficientInventory ,EventNotFound
from app.models.event import Event
from app.models.order import Order, OrderStatus 

def _key(event_id: int) -> str:
    """Key for an event's available seat counter."""
    return f"event:{event_id}:available"

async def set_initial_stock(
        redis: Redis,
        *,
        event_id: int,
        total_seats: int,
) -> None:
    """Initialize available counter when event launches. Idempotent (NX)."""
    await redis.set(_key(event_id), total_seats, nx=True)

async def get_available(
        redis: Redis,
        *,
        event_id: int,
) -> int:
    """Read current available count. Returns 0 if key missing or negative (race)."""
    val = await redis.get(_key(event_id))
    return max(0, int(val)) if val is not None else 0

async def reserve(
        redis: Redis,
        *,
        event_id: int,
        quantity: int,
) -> None:
    """Atomically reserve `quantity` seats. Raises InsufficientInventory on failure."""
    remaining = await redis.decrby(_key(event_id), quantity)
    if remaining < 0 :
        await redis.incrby(_key(event_id), quantity)
        available =remaining + quantity
        raise InsufficientInventory(
            event_id=event_id,
            requested=quantity,
            available=available,
        )
    
async def release(
        redis: Redis,
        *,
        event_id: int,
        quantity: int,
) -> None:
    """Return `quantity` seats to inventory (order expired/cancelled)."""
    await redis.incrby(_key(event_id), quantity)

async def reconcile_inventory(
        db: AsyncSession,
        redis: Redis,
        *,
        event_id: int,
) -> int:
    """從 Postgres 重算真實剩餘,覆蓋寫回 Redis。Redis 遺失後的權威重建。"""
    total_seats = await db.scalar(
        select(Event.total_seats).where(Event.id == event_id)
    )
    if total_seats is None:
        raise EventNotFound(event_id=event_id)
    
    held = (OrderStatus.PENDING, OrderStatus.PAID, OrderStatus.CONFIRMED)
    sold = await db.scalar(
        select(func.coalesce(func.sum(Order.quantity), 0))
        .where(Order.event_id == event_id, Order.status.in_(held))
    )

    remaining = total_seats - sold
    await redis.set(_key(event_id), remaining)
    return remaining

async def compute_expected_available(
        db: AsyncSession,
        *,
        event_id: int,
) -> int:
    """從 Postgres 算出『應有的剩餘庫存』—— 不碰 Redis,純讀。"""
    total_seats = await db.scalar(
        select(Event.total_seats).where(Event.id == event_id)
    )
    if total_seats is None:
        raise EventNotFound(event_id=event_id)
    
    held = (OrderStatus.PENDING, OrderStatus.PAID, OrderStatus.CONFIRMED)
    sold = await db.scalar(
        select(func.coalesce(func.sum(Order.quantity), 0))
        .where(Order.event_id == event_id, Order.status.in_(held))
    )
    return total_seats - sold

async def reconcile_inventory(
        db: AsyncSession,
        redis: Redis,
        *,
        event_id: int,
) -> int:
    """從 Postgres 重算真實剩餘,覆蓋寫回 Redis。"""
    remaining = await compute_expected_available(db, event_id=event_id)
    await redis.set(_key(event_id), remaining)
    return remaining