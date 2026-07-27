"""Inventory service — Redis-backed atomic seat counter.

Single source of truth for "how many seats remain". DB stores `events.total_seats`
as configuration; this module manages live decrement during sale.
"""
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy import  select, func

from redis.asyncio import Redis
from redis.commands.core import AsyncScript

from enum import StrEnum
from dataclasses import dataclass

from app.core.exceptions import InsufficientInventory ,EventNotFound,InventoryNotReconcilable
from app.models.event import Event
from app.models.order import Order, OrderStatus
from app.services.idempotency import _key as _claim_key
from app.services.queue_events import publish_event_poke

ORDER_STREAM_KEY="orders:stream"
ORDER_DEAD_LETTER_KEY="orders:stream:dead"



def _key(event_id: int) -> str:
    """Key for an event's available seat counter."""
    return f"event:{event_id}:available"

def _released_key(marker: str) -> str:
    """Idempotency marker for a single release event (guards double-release)."""
    return f"released:{marker}"

async def queue_depth(redis: Redis) -> tuple[int, int]:
    """回傳 (backlog, dead_letter):尚未落帳的 order intents、與永久失敗的。
    兩者皆為 0 代表 DB 已追上 Redis,是『DB 此刻可信』的訊號。
    """
    backlog = await redis.xlen(ORDER_STREAM_KEY)
    dead = await redis.xlen(ORDER_DEAD_LETTER_KEY)
    return backlog, dead

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
#   {'DUP'}                            -> this idempotency_key was already processed
#   {'SOLD_OUT', remaining}            -> not enough stock
#   {'OK', stream msg id, remaining}   -> seat reserved and order intent enqueued
#                                         (remaining lets the caller detect a sold-out crossing)
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

return {'OK', stream_id, tostring(avail - qty)}
"""

# Registered once on first use (SHA1 computed there, not per order), then reused.
# Constructing it needs a live client for the encoder, so it can't be built at import.
_reserve_script: AsyncScript | None = None


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
    if remaining == 0:                                   # just took the last seat(s)
        await publish_event_poke(redis, event_id)        # wake waiters -> they re-read -> sold_out

# Atomic idempotent release: mark-then-return-seats, all-or-nothing.
# KEYS[1]=released marker   KEYS[2]=stock key
# ARGV[1]=quantity          ARGV[2]=marker TTL seconds
# The SETNX marker makes a replayed release (crash between commit and release,
# stream re-delivery, double cancel) a no-op — seats are returned at most once.
# Returns (a tagged list, like the reserve script):
#   {'OK', new_available}  -> first release; seats returned
#   {'DUP'}                -> already released; no-op
# A tagged table (not a bare -1 sentinel) so a legitimate new count of -1 — reachable
# when reconcile writes negative stock during oversell recovery — isn't misread as DUP.
_RELEASE_LUA = """
if redis.call('SET', KEYS[1], '1', 'NX', 'EX', tonumber(ARGV[2])) then
    return {'OK', tostring(redis.call('INCRBY', KEYS[2], tonumber(ARGV[1])))}
end
return {'DUP'}
"""

# Registered once on first use (same pattern as the reserve script).
_release_script: AsyncScript | None = None


async def release(
        redis: Redis,
        *,
        event_id: int,
        quantity: int,
        marker: str,
        ttl_seconds: int = 86400,
) -> bool:
    """Return `quantity` seats to inventory — idempotently.

    `marker` uniquely identifies THIS release event; the `released:{marker}`
    SETNX guard returns the seats at most once even if the call is replayed
    (a crash between commit and release, a reclaimed stream entry, a double
    cancel). Use "order:{id}" for expire/cancel and "dl:{idempotency_key}" for
    dead-letter. Returns True if this call actually returned the seats, False
    if it was a no-op because they were already released.
    """
    global _release_script
    if _release_script is None:
        _release_script = redis.register_script(_RELEASE_LUA)   # SHA1 once
    result = await _release_script(
        keys=[_released_key(marker), _key(event_id)],
        args=[quantity, ttl_seconds],
        client=redis,
    )
    if result[0] == "DUP":
        return False                                     # replayed release — no-op, no crossing
    new_available = int(result[1])
    if new_available - quantity <= 0 < new_available:    # crossed from sold-out back to available
        await publish_event_poke(redis, event_id)        # wake waiters -> they re-read -> not sold_out
    return True

async def reconcile_inventory(
        db: AsyncSession,
        redis: Redis,
        *,
        event_id: int,
        force: bool = False,
) -> int:
    """從 Postgres 重算真實剩餘,覆蓋寫回 Redis。Redis 遺失後的權威重建。"""
    if not force:                                    # ← 守衛搬進來
        backlog, dead = await queue_depth(redis)
        # Only un-persisted backlog means inventory is genuinely in flight and the
        # DB SUM can't be trusted. dead-lettered intents are already settled
        # (batch 1: persisted → counted in the SUM; not persisted → seat refunded),
        # so dead>0 must NOT block reconcile — else one poison disables it forever.
        if backlog:
            raise InventoryNotReconcilable(event_id, backlog, dead)
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
    global _reserve_script
    if _reserve_script is None:
        _reserve_script = redis.register_script(_RESERVE_AND_ENQUEUE_LUA)   # SHA1 once
    result = await _reserve_script(
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
        client=redis,                       # current client (app or test)
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
    remaining = int(result[2])
    if remaining == 0:                                   # this reservation took the last seat(s)
        await publish_event_poke(redis, event_id)        # wake waiters -> they re-read -> sold_out
    return ReserveResult(
        outcome=ReserveOutcome.OK,
        stream_id=result[1],
    )
    