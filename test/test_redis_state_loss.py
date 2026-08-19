"""Redis 遺失狀態:偵測、自動重建、以及曝險窗的量化。

**這裡守的是整個系統唯一一條沒有對帳出口的路徑。** 庫存、限購額度、座位空段全部住在
Redis,而 `orders:stream` 裡的每一筆都是「已經扣了庫存、已經回了 202、還沒進
Postgres」的訂單。節點被換掉 / keyspace 被清 / 故障切換丟了尾端寫入時:

  - 庫存鍵不見了 → `get_available` 把缺鍵折成 0 → 那場**看起來完售**,而且所有計數器
    自洽,沒有任何訊號會亮。這是最惡劣的失敗模式:它不像故障,像是票賣完了。
  - stream 不見了 → 那些 202 的訂單人間蒸發,而且**沒有任何地方**可以列舉它們。
    能做的是把曝險窗量化並監控,而不是假裝它不存在。
"""
from datetime import datetime, timedelta, timezone

import pytest

from app.core.config import get_settings
from app.models.event import Event, EventStatus
from app.services.inventory import (
    ORDER_STREAM_KEY,
    get_available,
    oldest_intent_age_seconds,
    read_available,
    set_initial_stock,
    _key,
)
from app.worker import detect_inventory_drift


# ─ 缺鍵 vs 零

@pytest.mark.asyncio
async def test_missing_key_is_distinguishable_from_sold_out(redis, published_event):
    """`get_available` 把兩者折成同一個 0(熱路徑上那是安全的預設:表現成售完);
    `read_available` 必須分得出來,否則偵測那一側無從判斷是事故還是正常結局。"""
    await redis.set(_key(published_event.id), 0)
    assert await read_available(redis, event_id=published_event.id) == 0

    await redis.delete(_key(published_event.id))
    assert await read_available(redis, event_id=published_event.id) is None
    assert await get_available(redis, event_id=published_event.id) == 0   # 折成 0


# ─ 自動重建

@pytest.mark.asyncio
async def test_lost_inventory_key_is_rebuilt_from_postgres(db, redis, published_event):
    """庫存鍵不見 → 從 Postgres 重建,並記一筆 needs_human。

    重建的前提是無爭議的:backlog 為 0 時 Postgres 就是此刻的全部事實。不修的後果是
    那場永遠「完售」—— 而那不會觸發任何既有的告警。
    """
    await redis.delete(_key(published_event.id))

    drifts = await detect_inventory_drift({"redis_client": redis})

    assert drifts == [
        {"kind": "lost", "event_id": published_event.id, "expected": 5}
    ]
    assert await read_available(redis, event_id=published_event.id) == 5


@pytest.mark.asyncio
async def test_auto_heal_can_be_switched_off(db, redis, published_event, monkeypatch):
    """開關關掉時只回報、不重建 —— 給「想先看清楚發生什麼」的情境。"""
    monkeypatch.setattr(get_settings(), "AUTO_HEAL_LOST_REDIS_STATE", False)
    await redis.delete(_key(published_event.id))

    drifts = await detect_inventory_drift({"redis_client": redis})

    assert drifts[0]["kind"] == "lost"
    assert await read_available(redis, event_id=published_event.id) is None  # 沒有重建


@pytest.mark.asyncio
async def test_value_mismatch_is_reported_but_never_auto_fixed(db, redis, published_event):
    """值對不上**不能**自動修:那可能是某條釋放路徑的 bug,而自動修會把症狀每五分鐘
    擦掉一次,於是它永遠不會被查到。"""
    await redis.set(_key(published_event.id), 3)          # 應該是 5

    drifts = await detect_inventory_drift({"redis_client": redis})

    assert drifts == [{"event_id": published_event.id, "expected": 5, "actual": 3}]
    assert await read_available(redis, event_id=published_event.id) == 3   # 原封不動


@pytest.mark.asyncio
async def test_finished_events_are_not_reported_as_drift(db, redis):
    """已經過了保留期的場次,它的 Redis key 是**故意**被清掉的。

    少了時間窗過濾,那些場次每五分鐘都會被報成一次庫存漂移(Redis 沒鍵 → 0,而
    Postgres 還算得出未售出的張數)—— 一個永遠在響、而且完全「正確」地響的假警報,
    然後它會把真正的漂移淹掉。加上自動重建之後更糟:會把剛清掉的 key 重新建回來。
    """
    long_ago = datetime.now(timezone.utc) - timedelta(days=30)
    event = Event(
        name="Finished", venue="Old Arena",
        starts_at=long_ago, ends_at=long_ago + timedelta(hours=3),
        sale_starts_at=long_ago - timedelta(days=30),
        sale_ends_at=long_ago - timedelta(days=1),
        total_seats=100, price_cents=1500, status=EventStatus.PUBLISHED,
    )
    db.add(event)
    await db.commit()
    # key 已經被 purge_finished_event_keys 清掉了(所以這裡不 seed)

    assert await detect_inventory_drift({"redis_client": redis}) == []
    assert await read_available(redis, event_id=event.id) is None   # 沒有被重新建回來


# ─ 曝險窗

@pytest.mark.asyncio
async def test_empty_stream_has_no_exposure(redis):
    assert await oldest_intent_age_seconds(redis) == 0.0


@pytest.mark.asyncio
async def test_exposure_is_the_age_of_the_oldest_unpersisted_intent(redis):
    """曝險窗 = 最舊那一筆的年紀,不是最新那一筆。

    這個數字回答的是「Redis 此刻死掉,會丟掉多久以內收下的訂單」—— 而那取決於**最舊**
    的那一筆還沒落帳。年紀直接從 stream id 的毫秒時間戳算,不需要額外記帳。
    """
    await redis.xadd(ORDER_STREAM_KEY, {"n": "old"}, id="1000000000000-0")   # 2001-09-09
    await redis.xadd(ORDER_STREAM_KEY, {"n": "new"})

    # 用固定的 now 斷言,不依賴掛鐘:1000000000 秒 + 60 → 剛好 60 秒前
    age = await oldest_intent_age_seconds(redis, now=1000000060.0)

    assert age == pytest.approx(60.0, abs=0.01)


@pytest.mark.asyncio
async def test_exposure_is_reported_with_the_queue_stats(redis, published_event):
    """曝險窗必須跟 backlog 一起被送出去 —— 筆數回答「積了多少」,年紀回答「積了多久」,
    而只有後者能量化一次故障切換的損失。"""
    from app.worker import collect_queue_stats

    await set_initial_stock(redis, event_id=published_event.id, total_seats=5)
    await redis.xadd(ORDER_STREAM_KEY, {"n": "1"})

    stats = await collect_queue_stats(redis)

    assert stats["backlog"] == 1
    assert 0 <= stats["oldest_intent_age"] < 5
