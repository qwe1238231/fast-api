"""Outbox 寫入端:欠釋放的轉移(expire/cancel)必須在同一個交易掛上 seat.release 列。

這裡只測寫入端的不變量,relay 另有測試。四個不變量:
  1. expire 成功 → 恰好一筆未處理的 seat.release 列
  2. cancel 成功 → 同上
  3. CAS 輸掉 → 一筆都沒有(座位是贏家的責任,寫了就是替別人多還一次)
  4. rollback → 轉移和 outbox 列一起消失 —— 這一條就是 outbox 模式的全部意義:
     「訂單過期」和「欠一筆釋放」原子地同生共死
"""
from datetime import datetime, timedelta, timezone
from uuid import uuid4

import pytest
from sqlalchemy import func, select, update

from app.core.security import get_password_hash
from app.models.order import Order, OrderStatus
from app.models.outbox import SEAT_RELEASE, OutboxEntry
from app.models.user import User
from app.services.inventory import get_available, reserve
from app.services.orders import cancel_order, expire_order, release_order_seat
from app.worker import (
    OUTBOX_MAX_ATTEMPTS,
    OUTBOX_RETENTION_DAYS,
    process_outbox,
    purge_processed_outbox,
)


async def _persist_order(db, event) -> Order:
    user = User(
        username=f"buyer-{uuid4().hex[:8]}",
        hashed_password=get_password_hash("secret123"),
    )
    db.add(user)
    await db.flush()
    order = Order(
        user_id=user.id,
        event_id=event.id,
        quantity=1,
        total_price_cents=1000,
        idempotency_key=uuid4(),
        status=OrderStatus.PENDING,
    )
    db.add(order)
    await db.commit()
    return order


async def _outbox_rows(db, order_id: int) -> list[OutboxEntry]:
    return list(
        (
            await db.scalars(
                select(OutboxEntry).where(
                    OutboxEntry.aggregate_type == "order",
                    OutboxEntry.aggregate_id == order_id,
                )
            )
        ).all()
    )


@pytest.mark.asyncio
async def test_expire_enqueues_seat_release(db, published_event):
    order = await _persist_order(db, published_event)

    assert await expire_order(db, order) is True
    await db.commit()

    rows = await _outbox_rows(db, order.id)
    assert len(rows) == 1
    assert rows[0].event_type == SEAT_RELEASE
    assert rows[0].processed_at is None
    assert rows[0].payload == {}


@pytest.mark.asyncio
async def test_cancel_enqueues_seat_release(db, published_event):
    order = await _persist_order(db, published_event)

    assert await cancel_order(db, order) is True
    await db.commit()

    rows = await _outbox_rows(db, order.id)
    assert len(rows) == 1
    assert rows[0].event_type == SEAT_RELEASE


@pytest.mark.asyncio
async def test_lost_cas_enqueues_nothing(db, published_event):
    order = await _persist_order(db, published_event)
    # 繞過 ORM 直接把列改走,模擬「讀到 PENDING 之後別人先轉移」:
    # ORM 物件還以為是 PENDING(合法轉移),但 CAS 的 WHERE status='pending'
    # 撈不到列 → False。這是 race,不是 InvalidOrderTransition。
    await db.execute(
        update(Order)
        .where(Order.id == order.id)
        # ck_orders_paid_at:status='paid' 必須帶 paid_at —— 模擬也得是合法狀態
        .values(status=OrderStatus.PAID, paid_at=func.now())
        .execution_options(synchronize_session=False)
    )

    assert await expire_order(db, order) is False
    await db.commit()

    assert await _outbox_rows(db, order.id) == []


@pytest.mark.asyncio
async def test_rollback_discards_transition_and_outbox_together(db, published_event):
    order = await _persist_order(db, published_event)
    # rollback 會 expire ORM 物件(expire_on_commit=False 只管 commit),之後再摸
    # order.id 會觸發同步 lazy refresh → MissingGreenlet。先把 id 拿出來。
    order_id = order.id

    assert await expire_order(db, order) is True
    await db.rollback()

    assert await _outbox_rows(db, order_id) == []
    status = await db.scalar(select(Order.status).where(Order.id == order_id))
    assert status == OrderStatus.PENDING


# ---- relay(worker.process_outbox):at-least-once 消費端 ----


@pytest.mark.asyncio
async def test_relay_releases_when_fast_path_missed(db, redis, published_event):
    """fast path 沒跑(模擬 commit 後崩掉)→ relay 把座位還回去。這就是整個模式的
    存在理由:沒有 relay 之前,這個場景 = 票從世界上消失。"""
    order = await _persist_order(db, published_event)
    await reserve(redis, event_id=published_event.id, quantity=1)   # 5 -> 4

    assert await expire_order(db, order) is True
    await db.commit()                       # 刻意不呼叫 release_order_seat

    await process_outbox({"redis_client": redis})

    assert await get_available(redis, event_id=published_event.id) == 5
    rows = await _outbox_rows(db, order.id)
    assert rows[0].processed_at is not None
    assert rows[0].attempts == 1            # claim 過一次


@pytest.mark.asyncio
async def test_relay_is_noop_when_fast_path_succeeded(db, redis, published_event):
    """fast path 已經還過 → relay 重放必須是 no-op(SETNX marker 擋住),
    不能把庫存多加一次。"""
    order = await _persist_order(db, published_event)
    await reserve(redis, event_id=published_event.id, quantity=1)   # 5 -> 4

    assert await expire_order(db, order) is True
    await db.commit()
    await release_order_seat(db, redis, order)                      # 4 -> 5 (fast path)

    await process_outbox({"redis_client": redis})

    assert await get_available(redis, event_id=published_event.id) == 5   # 不是 6
    rows = await _outbox_rows(db, order.id)
    assert rows[0].processed_at is not None


@pytest.mark.asyncio
async def test_relay_leaves_unknown_type_for_retry_with_backoff(db, redis):
    """沒有 handler 的列:不標記、attempts+1、租約推向未來 —— 第二輪跑立刻再跑
    不會重複 claim(退避生效)。"""
    db.add(
        OutboxEntry(
            event_type="bogus.event",
            aggregate_type="order",
            aggregate_id=999_999,
            payload={},
        )
    )
    await db.commit()

    await process_outbox({"redis_client": redis})
    await process_outbox({"redis_client": redis})   # 立刻重跑:租約未到期,claim 不到

    rows = await _outbox_rows(db, 999_999)
    assert rows[0].processed_at is None
    assert rows[0].attempts == 1


@pytest.mark.asyncio
async def test_relay_skips_dead_pile(db, redis):
    """attempts 耗盡的列不再被 claim —— 它是 needs_human 的 dead pile,
    不是重試佇列的一員。"""
    db.add(
        OutboxEntry(
            event_type=SEAT_RELEASE,
            aggregate_type="order",
            aggregate_id=999_998,
            payload={},
            attempts=OUTBOX_MAX_ATTEMPTS,
        )
    )
    await db.commit()

    await process_outbox({"redis_client": redis})

    rows = await _outbox_rows(db, 999_998)
    assert rows[0].processed_at is None
    assert rows[0].attempts == OUTBOX_MAX_ATTEMPTS   # 沒被 claim 動過


@pytest.mark.asyncio
async def test_purge_keeps_unprocessed_rows(db, redis):
    """purge 只清過了保留期的已處理列;未處理的列是債,永不清。"""
    old = datetime.now(timezone.utc) - timedelta(days=OUTBOX_RETENTION_DAYS + 1)
    db.add(
        OutboxEntry(
            event_type=SEAT_RELEASE,
            aggregate_type="order",
            aggregate_id=1_000_001,
            payload={},
            processed_at=old,
        )
    )
    db.add(
        OutboxEntry(
            event_type=SEAT_RELEASE,
            aggregate_type="order",
            aggregate_id=1_000_002,
            payload={},
        )
    )
    await db.commit()

    await purge_processed_outbox({})

    assert await _outbox_rows(db, 1_000_001) == []
    survivors = await _outbox_rows(db, 1_000_002)
    assert len(survivors) == 1
