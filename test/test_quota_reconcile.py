"""限購額度的對帳與漂移偵測。

**為什麼這件事值得自己的一個檔案:額度漂移比庫存漂移更難發現。**
超賣會撞到總量 —— 等候室的 sold_out 不觸發、`available` 變負、漂移偵測會叫。
超買不會撞到任何東西:那個人就是多買了幾張,而所有計數器都自洽,`available` 是對的
(它剛被重建過)。所以除了這裡,沒有任何機制會看到它。

最惡的形狀是 **Redis 遺失之後的靜默重置**:`reconcile_inventory` 只重建 `available`
的話,每個人的額度都歸零 —— 已經買滿的人可以再買一輪,而庫存看起來完全正常。
"""
from datetime import datetime, timedelta, timezone
from uuid import uuid4

import pytest
from sqlalchemy import select

from app.core.config import get_settings
from app.models.event import Event, EventStatus
from app.models.order import Order, OrderStatus
from app.models.user import User
from app.services.inventory import (
    ReserveOutcome,
    _purchased_key,
    compute_expected_quotas,
    read_purchase_quotas,
    reconcile_inventory,
    reserve_and_enqueue,
)
from app.worker import detect_inventory_drift

LIMIT = get_settings().MAX_TICKETS_PER_USER_PER_EVENT


async def _event(db, redis, *, seats: int = 100) -> Event:
    """自己建一個庫存充足的場次。

    不用 `published_event`(5 席)是因為這個檔案測的是**額度**:5 席很容易被測試資料
    買完,而庫存檢查先於限購檢查(刻意的順序),於是拿到的會是 SOLD_OUT —— 測試就變成
    在測庫存而不是額度。
    """
    now = datetime.now(timezone.utc)
    event = Event(
        name=f"Quota {uuid4().hex[:6]}", venue="X",
        starts_at=now + timedelta(days=1), ends_at=now + timedelta(days=1, hours=2),
        sale_starts_at=now - timedelta(hours=1), sale_ends_at=now + timedelta(hours=1),
        total_seats=seats, price_cents=100, status=EventStatus.PUBLISHED,
    )
    db.add(event)
    await db.flush()
    await redis.set(f"event:{event.id}:available", seats)
    return event


async def _buyer(db, name: str) -> int:
    user = User(username=name, hashed_password="x")
    db.add(user)
    await db.flush()
    return user.id


async def _persisted_order(db, *, event_id: int, user_id: int, qty: int,
                           status: OrderStatus = OrderStatus.PENDING) -> Order:
    """直接寫一列訂單 —— 模擬 worker 已經落帳的狀態。"""
    now = datetime.now(timezone.utc)
    stamps = {
        OrderStatus.PAID: {"paid_at": now},
        OrderStatus.CONFIRMED: {"confirmed_at": now},
        OrderStatus.EXPIRED: {"expired_at": now},
        OrderStatus.CANCELLED: {"cancelled_at": now},
    }.get(status, {})
    order = Order(
        user_id=user_id, event_id=event_id, quantity=qty,
        total_price_cents=100 * qty, idempotency_key=uuid4(), status=status, **stamps,
    )
    db.add(order)
    await db.commit()
    return order


@pytest.mark.asyncio
async def test_reconcile_rebuilds_the_quota_hash_after_redis_loss(db, redis) -> None:
    """**這是整件事的核心情境。**

    Redis 遺失 → 只重建 `available` 的話,額度全部歸零,已經買滿的人可以再買一輪,
    而庫存數字看起來完全正常(它剛從 Postgres 重算過)。沒有任何告警會亮。
    """
    event = await _event(db, redis)
    alice = await _buyer(db, "alice-q")
    bob = await _buyer(db, "bob-q")
    await _persisted_order(db, event_id=event.id, user_id=alice, qty=LIMIT)
    await _persisted_order(db, event_id=event.id, user_id=bob, qty=1)

    # 模擬 Redis 整個不見了
    await redis.flushdb()
    assert await read_purchase_quotas(redis, event_id=event.id) == {}

    await reconcile_inventory(db, redis, event_id=event.id)

    assert await read_purchase_quotas(redis, event_id=event.id) == {
        str(alice): LIMIT, str(bob): 1,
    }
    # 而且額度真的生效:alice 買滿了,不能再買
    result = await reserve_and_enqueue(
        redis, event_id=event.id, user_id=alice, quantity=1,
        total_price_cents=100, idempotency_key=str(uuid4()),
    )
    assert result.outcome == ReserveOutcome.OVER_LIMIT


@pytest.mark.asyncio
async def test_reconcile_drops_ghost_entries(db, redis) -> None:
    """整體覆寫而不是逐欄位修正 —— 因為要清掉的是**多出來的**欄位。

    幽靈欄位的來源:某個人的訂單全被取消了,但退額度那一步沒跑到(釋放路徑拋例外)。
    逐欄位比對永遠看不到它,而它的效果正是「那個人再也買不了這場」—— 一個沒有任何
    錯誤訊息的永久封鎖。
    """
    event = await _event(db, redis)
    ghost = await _buyer(db, "ghost-q")
    await redis.hset(_purchased_key(event.id), str(ghost), LIMIT)

    await reconcile_inventory(db, redis, event_id=event.id)

    assert await read_purchase_quotas(redis, event_id=event.id) == {}, (
        "DB 裡沒有這個人的訂單,額度就不該留著"
    )


@pytest.mark.asyncio
async def test_terminal_orders_do_not_hold_quota(db, redis) -> None:
    """過期/取消的訂單不佔額度 —— 對帳要跟下單路徑用同一組狀態。"""
    event = await _event(db, redis)
    user = await _buyer(db, "mixed-q")
    await _persisted_order(db, event_id=event.id, user_id=user, qty=1)
    await _persisted_order(db, event_id=event.id, user_id=user, qty=2,
                           status=OrderStatus.CONFIRMED)
    await _persisted_order(db, event_id=event.id, user_id=user, qty=3,
                           status=OrderStatus.EXPIRED)
    await _persisted_order(db, event_id=event.id, user_id=user, qty=4,
                           status=OrderStatus.CANCELLED)

    expected = await compute_expected_quotas(db, event_id=event.id)
    assert expected == {str(user): 3}, "只算 pending + paid + confirmed"

    await reconcile_inventory(db, redis, event_id=event.id)
    assert await read_purchase_quotas(redis, event_id=event.id) == {str(user): 3}


@pytest.mark.asyncio
async def test_reconcile_refuses_while_intents_are_in_flight(db, redis) -> None:
    """未排空的 stream 讓 DB 的事實不完整 —— 額度跟庫存共用同一道守衛。

    少了它,對帳會把「已扣 Redis 額度、還沒寫進 DB」的 in-flight 訂單當成不存在而
    把額度清掉,那個人就能再買一輪。
    """
    from app.core.exceptions import InventoryNotReconcilable

    event = await _event(db, redis)
    user = await _buyer(db, "inflight-q")
    await reserve_and_enqueue(     # 只進 Redis 與 stream,沒有落帳
        redis, event_id=event.id, user_id=user, quantity=2,
        total_price_cents=200, idempotency_key=str(uuid4()),
    )
    with pytest.raises(InventoryNotReconcilable):
        await reconcile_inventory(db, redis, event_id=event.id)


# ─ 漂移偵測

@pytest.mark.asyncio
async def test_drift_detection_notices_a_quota_mismatch(db, redis) -> None:
    """額度對不上要被記錄。這是唯一會看到超買的地方。"""
    event = await _event(db, redis)
    user = await _buyer(db, "drifter-q")
    await _persisted_order(db, event_id=event.id, user_id=user, qty=2)
    await redis.set(f"event:{event.id}:available", 98)           # 庫存是對的
    await redis.hset(_purchased_key(event.id), str(user), 99)    # 額度不是

    drifts = await detect_inventory_drift({"redis_client": redis})

    quota = [d for d in drifts if d.get("kind") == "quota"]
    assert quota, f"沒有偵測到額度漂移:{drifts}"
    assert quota[0]["event_id"] == event.id
    assert quota[0]["sample"][str(user)] == (99, 2), "要帶 (redis, expected) 方便判讀"


@pytest.mark.asyncio
async def test_drift_detection_is_quiet_when_quotas_agree(db, redis) -> None:
    """一致的時候不能叫 —— 一個會誤報的漂移偵測最後會被關掉。"""
    event = await _event(db, redis)
    user = await _buyer(db, "clean-q")
    await _persisted_order(db, event_id=event.id, user_id=user, qty=2)
    await redis.set(f"event:{event.id}:available", 98)
    await redis.hset(_purchased_key(event.id), str(user), 2)

    drifts = await detect_inventory_drift({"redis_client": redis})
    assert [d for d in drifts if d.get("kind") == "quota"] == []
