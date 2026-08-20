"""已結束場次的 Redis key 清理。

這些 key 刻意都沒有 TTL —— 給 `available` 設 TTL 會讓庫存在開賣中途消失。所以只能
靠排程清,而排程的正確性只能靠測試守:清錯會刪掉正在賣的場次的庫存。
"""
from datetime import datetime, timedelta, timezone

import pytest
import pytest_asyncio
from sqlalchemy import select

from app.models.event import Event, EventStatus
from app.models.seating import EventZonePrice, Zone
from app.scripts.seed_venue import RowSpec, VenueSpec, ZoneSpec, seed_venue
from app.services.inventory import _key as _event_available_key, _purchased_key
from app.services.publish_event import publish_event
from app.services.seat_runs import (
    _ends_key,
    _geom_key,
    _runs_key,
    _zone_available_key,
)
from app.worker import (
    EVENT_KEY_PURGE_WINDOW_DAYS,
    EVENT_KEY_RETENTION_DAYS,
    purge_finished_event_keys,
)

pytestmark = pytest.mark.asyncio


@pytest_asyncio.fixture
async def make_event(db, redis):
    """建一個已發佈的座位場次,ends_at 可以指定成過去。"""
    counter = {"n": 0}

    async def _make(*, ends_days_ago: float | None = None) -> tuple[int, int]:
        counter["n"] += 1
        spec = VenueSpec(
            name=f"Purge Arena {counter['n']}",
            zones=(ZoneSpec(name="A區", display_order=0, rows=(RowSpec("A", (6,)),)),),
        )
        venue = await seed_venue(db, spec)
        zone_id = await db.scalar(select(Zone.id).where(Zone.venue_id == venue.id))
        now = datetime.now(timezone.utc)
        event = Event(
            name=spec.name, venue=spec.name, venue_id=venue.id,
            starts_at=now + timedelta(days=1), ends_at=now + timedelta(days=1, hours=2),
            sale_starts_at=now - timedelta(hours=1), sale_ends_at=now + timedelta(hours=1),
            total_seats=6, price_cents=100, status=EventStatus.DRAFT,
        )
        db.add(event)
        await db.flush()
        db.add(EventZonePrice(event_id=event.id, zone_id=zone_id, price_cents=1000))
        await publish_event(db, redis, event)     # 先用未來的 ends_at 正常發佈
        # 限購計數器是**買了才存在**的(publish 不會建),所以這裡補一筆假的持有。
        # 少了它,`_all_keys` 對這把 key 的「清乾淨了」斷言會因為它從未存在而空過。
        await redis.hset(_purchased_key(event.id), "1", "2")
        if ends_days_ago is not None:
            # 兩個都要往回搬:只動 ends_at 會造出「結束早於開演」的場次,而那是
            # ck_events_show_window 擋掉的東西 —— 真實世界裡也不存在。
            event.ends_at = now - timedelta(days=ends_days_ago)
            event.starts_at = event.ends_at - timedelta(hours=2)
        await db.commit()
        return event.id, zone_id

    return _make


def _all_keys(event_id: int, zone_id: int) -> list[str]:
    return [
        _event_available_key(event_id),
        _purchased_key(event_id),
        f"queue:{event_id}:admit_start",
        f"queue:{event_id}:salt",
        _runs_key(event_id, zone_id),
        _ends_key(event_id, zone_id),
        _geom_key(event_id, zone_id),
        _zone_available_key(event_id, zone_id),
    ]


async def _present(redis, keys: list[str]) -> list[str]:
    return [k for k in keys if await redis.exists(k)]


async def test_a_live_event_keeps_its_keys(redis, make_event) -> None:
    """最重要的一條:清理絕不能碰到正在賣的場次。"""
    event_id, zone_id = await make_event()
    keys = _all_keys(event_id, zone_id)
    assert await _present(redis, keys) == keys, "前提:發佈後這些 key 都存在"

    assert await purge_finished_event_keys({"redis_client": redis}) == 0
    assert await _present(redis, keys) == keys


async def test_an_event_inside_the_grace_window_keeps_its_keys(redis, make_event) -> None:
    """結束了但還在保留期內 —— 留給事後對帳。"""
    event_id, zone_id = await make_event(ends_days_ago=EVENT_KEY_RETENTION_DAYS - 1)
    keys = _all_keys(event_id, zone_id)

    assert await purge_finished_event_keys({"redis_client": redis}) == 0
    assert await _present(redis, keys) == keys


async def test_a_finished_event_is_purged(redis, make_event) -> None:
    event_id, zone_id = await make_event(ends_days_ago=EVENT_KEY_RETENTION_DAYS + 1)
    keys = _all_keys(event_id, zone_id)
    assert await _present(redis, keys) == keys

    assert await purge_finished_event_keys({"redis_client": redis}) > 0
    assert await _present(redis, keys) == []


async def test_purge_is_idempotent(redis, make_event) -> None:
    await make_event(ends_days_ago=EVENT_KEY_RETENTION_DAYS + 1)
    assert await purge_finished_event_keys({"redis_client": redis}) > 0
    assert await purge_finished_event_keys({"redis_client": redis}) == 0


async def test_events_older_than_the_window_are_skipped(redis, make_event) -> None:
    """上界讓每天的工作量有界(不必重掃整個 events 表),代價是 worker 停機超過
    (WINDOW − RETENTION) 天的話那段期間的 key 會留下。

    這條測試把那個取捨變成明文,並記下補救方式:手動放大 window_days 重跑一次。
    """
    event_id, zone_id = await make_event(
        ends_days_ago=EVENT_KEY_PURGE_WINDOW_DAYS + 5
    )
    keys = _all_keys(event_id, zone_id)

    assert await purge_finished_event_keys({"redis_client": redis}) == 0
    assert await _present(redis, keys) == keys

    # 補救:放大窗口重跑。
    assert await purge_finished_event_keys(
        {"redis_client": redis}, window_days=EVENT_KEY_PURGE_WINDOW_DAYS + 30
    ) > 0
    assert await _present(redis, keys) == []


async def test_only_the_finished_event_is_purged(redis, make_event) -> None:
    """兩個場次共存時,只清該清的那個 —— 這是「清錯會刪掉營運中庫存」的直接防線。"""
    live_id, live_zone = await make_event()
    old_id, old_zone = await make_event(ends_days_ago=EVENT_KEY_RETENTION_DAYS + 2)

    assert await purge_finished_event_keys({"redis_client": redis}) > 0
    assert await _present(redis, _all_keys(old_id, old_zone)) == []
    assert await _present(redis, _all_keys(live_id, live_zone)) == _all_keys(
        live_id, live_zone
    )


async def test_unseated_event_keys_are_purged_too(db, redis, published_event) -> None:
    """無座位圖的舊場次只有 counter 與 queue 兩組 key,一樣要清。"""
    published_event.ends_at = datetime.now(timezone.utc) - timedelta(
        days=EVENT_KEY_RETENTION_DAYS + 1
    )
    published_event.starts_at = published_event.ends_at - timedelta(hours=2)
    await db.commit()
    key = _event_available_key(published_event.id)
    assert await redis.exists(key)

    assert await purge_finished_event_keys({"redis_client": redis}) > 0
    assert not await redis.exists(key)


async def test_every_per_event_key_is_covered(redis, make_event) -> None:
    """清單漏一個 key 就等於永久洩漏。上一版漏掉 queue:{e}:admit_start,而漏掉的
    原因是在 worker 裡**重打** key 格式而不是 import waiting_room 的 helper ——
    這條測試枚舉 waiting_room 與 seat_runs 自己宣告的所有 per-event key。
    """
    from app.services.seat_runs import (
        _ends_key, _geom_key, _relaxed_key, _runs_key, _zone_available_key,
    )
    from app.services.waiting_room import _admit_start_key, _draw_key, _salt_key

    event_id, zone_id = await make_event(ends_days_ago=EVENT_KEY_RETENTION_DAYS + 1)
    declared = [
        _event_available_key(event_id),
        _salt_key(event_id), _draw_key(event_id), _admit_start_key(event_id),
        _runs_key(event_id, zone_id), _ends_key(event_id, zone_id),
        _geom_key(event_id, zone_id), _zone_available_key(event_id, zone_id),
        _relaxed_key(event_id, zone_id),
    ]
    for key in declared:                       # relaxed 平常不存在,補上才測得到
        if not await redis.exists(key):
            await redis.set(key, "1")

    assert await purge_finished_event_keys({"redis_client": redis}) > 0
    assert await _present(redis, declared) == []
