"""Inventory service — Redis-backed atomic seat counter.

Single source of truth for "how many seats remain". DB stores `events.total_seats`
as configuration; this module manages live decrement during sale.
"""
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy import  select, func

from redis.asyncio import Redis

from enum import StrEnum
from dataclasses import dataclass

from app.core.exceptions import InsufficientInventory ,EventNotFound
from app.models.event import Event
from app.models.order import Order, OrderStatus 
from app.services.idempotency import _key as _claim_key

ORDER_STREAM_KEY="orders:stream"
ORDER_DEAD_LETTER_KEY="orders:stream:dead"

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

# Atomic script: dedup + decrement stock + enqueue + write claim, all-or-nothing.
# KEYS[1]=stock key   KEYS[2]=claim key   KEYS[3]=stream key
# ARGV[1]=quantity  ARGV[2]=claim TTL seconds  ARGV[3]=user_id
# ARGV[4]=event_id  ARGV[5]=total_price_cents  ARGV[6]=idempotency_key
# Returns (a list on the Python side):
#   {'DUP'}                 -> this idempotency_key was already processed
#   {'SOLD_OUT', remaining} -> not enough stock
#   {'OK', stream msg id}   -> seat reserved and order intent enqueued
_RESERVE_AND_ENQUEUE_LUA = """
local qty = tonumber(ARGV[1])

-- 1) Dedup: if the claim already exists, return DUP and never decrement.
if redis.call('EXISTS', KEYS[2]) == 1 then
    return {'DUP'}
end

-- 2) Read stock and check sold-out (GET on a missing key yields false -> nil after tonumber).
local avail = tonumber(redis.call('GET', KEYS[1]))
if avail == nil or avail < qty then
    return {'SOLD_OUT', tostring(avail or 0)}
end

-- 3) Decrement stock.
redis.call('DECRBY', KEYS[1], qty)

-- 4) Push the order intent into the stream, capture the auto-generated message id.
local stream_id = redis.call('XADD', KEYS[3], '*',
    'user_id', ARGV[3],
    'event_id', ARGV[4],
    'quantity', ARGV[1],
    'total_price_cents', ARGV[5],
    'idempotency_key', ARGV[6])

-- 5) Write the claim (with TTL) so the next request with the same key is blocked at step 1.
redis.call('SET', KEYS[2], 'PENDING', 'EX', tonumber(ARGV[2]))

return {'OK', stream_id}
"""


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

class ReserveOutcome(StrEnum):
    """The three possible outcomes of reserve_and_enqueue."""
    OK = "OK"
    DUP = "DUP"
    SOLD_OUT = "SOLD_OUT"


@dataclass(frozen=True)
class ReserveResult:
    outcome: ReserveOutcome
    stream_id: str | None = None   # set only when outcome == OK
    available: int | None = None   # set only when outcome == SOLD_OUT


async def reserve_and_enqueue(
        redis: Redis,
        *,
        event_id: int,
        user_id: int,
        quantity: int,
        total_price_cents: int,
        idempotency_key: str,
        claim_ttl_seconds: int = 86400,
) -> ReserveResult:
    """Atomically: dedup + decrement stock + enqueue + write claim.

    Everything runs inside one Lua script, so no other request can interleave --
    that is what welds "decrement stock" and "enqueue" into a single action and
    closes the gap.
    """
    script = redis.register_script(_RESERVE_AND_ENQUEUE_LUA)
    result = await script(
        keys=[
            _key(event_id),                 # KEYS[1] stock
            _claim_key(idempotency_key),    # KEYS[2] claim
            ORDER_STREAM_KEY,               # KEYS[3] stream
        ],
        args=[
            quantity,                       # ARGV[1]
            claim_ttl_seconds,              # ARGV[2]
            user_id,                        # ARGV[3]
            event_id,                       # ARGV[4]
            total_price_cents,              # ARGV[5]
            idempotency_key,                # ARGV[6]
        ],
    )

    # client has decode_responses=True, so result items are already str (not bytes).
    status = result[0]
    if status == ReserveOutcome.DUP:
        return ReserveResult(outcome=ReserveOutcome.DUP)
    if status == ReserveOutcome.SOLD_OUT:
        return ReserveResult(
            outcome=ReserveOutcome.SOLD_OUT,
            available=int(result[1]),
        )
    return ReserveResult(
        outcome=ReserveOutcome.OK,
        stream_id=result[1],
    )
    