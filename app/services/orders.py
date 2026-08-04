"""Order service — state transition side effects.

CRUD layer (app/crud/order.py) does the raw DB op AND enforces which
transitions are legal (the state machine lives there, next to the mutation).
This layer orchestrates the side effects each transition entails
(e.g. releasing Redis inventory on cancel/expire).
"""
from datetime import datetime, timezone
from redis.asyncio import Redis as RedisClient
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession
from uuid import UUID

from app.core.exceptions import (
    EventCancelled, EventNotOnSale, EventNotFound, InsufficientInventory,
)
from app.crud.order import transition_order_status
from app.models.order import Order, OrderStatus
from app.models.seating import SeatHold
from app.services.inventory import release, reserve_and_enqueue, ReserveOutcome, ReserveResult
from app.models.event import EventStatus
from app.services.event_cache import get_event_meta
from app.services.pricing import total_for
from app.services.seat_runs import release_seats, reserve_seats_and_enqueue

async def mark_paid(db: AsyncSession, order: Order) -> bool:
    """CAS PENDING -> PAID. Returns True iff applied (False = order left PENDING)."""
    return await transition_order_status(db, order, OrderStatus.PAID)


async def mark_confirmed(db: AsyncSession, order: Order) -> bool:
    """CAS PAID -> CONFIRMED. Returns True iff applied."""
    return await transition_order_status(db, order, OrderStatus.CONFIRMED)


async def cancel_order(db: AsyncSession, order: Order) -> bool:
    """CAS PENDING -> CANCELLED. Returns True iff applied.

    Like expire_order, the seat release is a POST-COMMIT step owned by the caller
    (the endpoint): pair a True return with `db.commit()` then release_order_seat().
    """
    return await transition_order_status(db, order, OrderStatus.CANCELLED)


async def expire_order(db: AsyncSession, order: Order) -> bool:
    """CAS the order PENDING -> EXPIRED. Returns True iff applied.

    The seat release is deliberately NOT done here: it must happen AFTER the
    caller commits (a rolled-back transition must never leave a released seat),
    and the commit is owned by the caller (the expire cron). The caller pairs a
    True return with `db.commit()` then `release_order_seat()`.
    """
    return await transition_order_status(db, order, OrderStatus.EXPIRED)


async def release_order_seat(
    db: AsyncSession, redis: RedisClient, order: Order
) -> bool:
    """Idempotently return an order's seat(s) to inventory — a POST-COMMIT step.

    Keyed by the order id, so the same order's seat is returned at most once
    (guards double-release across cancel/expire/retries/crashes). Returns True
    if this call actually returned the seat, False if it was already released.

    座位場次要還的是一段具體區間,所以得先讀出 SeatHold。順序是 **DB 先放手、
    再改 Redis**:
      - DB 先 → 崩在中間的話 hold 沒了但 Redis 還佔著,那些位子直到
        rebuild_zone_runs 之前賣不掉。可用性損失,不會有人重複售出。
      - Redis 先 → 崩在中間的話別人搶到這些位子,worker 落帳時撞上殘留的 hold
        列而 dead-letter。同樣是可用性損失,但額外製造了失敗的訂單。
    所以選 DB 先。Redis 那半由 `released:{marker}` 的 SETNX 保證冪等。
    """
    marker = f"order:{order.id}"
    if order.zone_id is None:
        return await release(
            redis,
            event_id=order.event_id,
            quantity=order.quantity,
            marker=marker,
        )

    hold = await db.scalar(select(SeatHold).where(SeatHold.order_id == order.id))
    if hold is None:
        return False        # 已經還過了(或 intent 從未落帳)—— 冪等 no-op
    block_id, start_pos, length = hold.block_id, hold.start_pos, hold.length
    await db.delete(hold)
    await db.commit()
    return await release_seats(
        redis,
        event_id=order.event_id,
        zone_id=order.zone_id,
        block_id=block_id,
        start_pos=start_pos,
        length=length,
        marker=marker,
    )


async def submit_order(
        db: AsyncSession,
        redis: RedisClient,
        *,
        user_id: int,
        event_id: int,
        quantity: int,
        idempotency_key: UUID,
        zone_id: int | None = None,
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

    # 金額一律走 pricing:分區票價與單一票價會長期共存(座位圖 migration 是純
    # 加法),呼叫端不該知道差別。無座位圖的場次行為與改動前完全相同。
    total_price_cents = total_for(event, zone_id=zone_id, quantity=quantity)

    if event.venue_id is not None:
        # 座位場次:配一段連續座位。zone_id 一定不是 None —— total_for 已經在上面
        # 用 ZoneRequired 擋掉了,所以這裡不需要再檢查一次。
        assert zone_id is not None
        reservation = await reserve_seats_and_enqueue(
            redis,
            event_id=event_id,
            zone_id=zone_id,
            user_id=user_id,
            quantity=quantity,
            total_price_cents=total_price_cents,
            idempotency_key=str(idempotency_key),
        )
        # 回傳型別維持 ReserveResult:座號在確認前不對外揭露,呼叫端不該拿到它
        # (那是 pending hold 可以被 compaction 滑動的前提)。
        if reservation is None:
            return ReserveResult(outcome=ReserveOutcome.DUP)
        return ReserveResult(
            outcome=ReserveOutcome.OK, stream_id=reservation.stream_id
        )

    # 無座位圖的場次:純計數器的舊路徑,一行都不改。
    result = await reserve_and_enqueue(
        redis,
        event_id=event_id,
        user_id=user_id,
        quantity=quantity,
        total_price_cents=total_price_cents,
        idempotency_key=str(idempotency_key),
        zone_id=zone_id,
    )
    if result.outcome == ReserveOutcome.SOLD_OUT:
        raise InsufficientInventory(
            event_id=event_id,
            requested=quantity,
            available=result.available,
        )
    return result