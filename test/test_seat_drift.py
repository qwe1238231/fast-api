"""座位結構的漂移偵測。

這是「預防勝於復原」的那一半。結構一旦損壞,最終會表現成 worker 落帳時撞上
seat_holds 的 EXCLUDE —— 但那已經太晚:那時 Redis 已經把重疊的座位發給兩個買家。
這三條檢查是同一件事的早期訊號,所以每一條都要有測試證明它真的抓得到。
"""
from datetime import datetime, timedelta, timezone
from uuid import uuid4

import pytest
import pytest_asyncio
from sqlalchemy import select

from app.models.event import Event, EventStatus
from app.models.order import Order, OrderStatus
from app.models.seating import EventZonePrice, SeatBlock, SeatHold, Zone
from app.models.user import User
from app.scripts.seed_venue import RowSpec, VenueSpec, ZoneSpec, seed_venue
from app.services.publish_event import publish_event
from app.services.seat_runs import (
    _ends_key,
    _runs_key,
    _zone_available_key,
    reserve_seats_and_enqueue,
)
from app.worker import detect_seat_structure_drift

pytestmark = pytest.mark.asyncio


@pytest_asyncio.fixture
async def seated(db, redis):
    spec = VenueSpec(
        name="Drift Arena",
        zones=(ZoneSpec(name="A區", display_order=0, rows=(RowSpec("A", (12,)),)),),
    )
    venue = await seed_venue(db, spec)
    zone_id = await db.scalar(select(Zone.id).where(Zone.venue_id == venue.id))
    now = datetime.now(timezone.utc)
    event = Event(
        name="Drift Show", venue=spec.name, venue_id=venue.id,
        starts_at=now + timedelta(days=1), ends_at=now + timedelta(days=1, hours=2),
        sale_starts_at=now - timedelta(hours=1), sale_ends_at=now + timedelta(hours=1),
        total_seats=12, price_cents=100, status=EventStatus.DRAFT,
    )
    user = User(username="drifter", hashed_password="x")
    db.add_all([event, user])
    await db.flush()
    db.add(EventZonePrice(event_id=event.id, zone_id=zone_id, price_cents=1000))
    await publish_event(db, redis, event)
    await db.commit()
    return {"event_id": event.id, "zone_id": zone_id, "user_id": user.id}


async def _drift(redis) -> list[dict]:
    return await detect_seat_structure_drift({"redis_client": redis})


# ─ 乾淨狀態

async def test_a_freshly_published_zone_has_no_drift(redis, seated) -> None:
    assert await _drift(redis) == []


async def test_no_drift_after_real_allocations(redis, seated) -> None:
    for buyer in range(3):
        # 三個不同買家:三筆兩人票共 6 張,同一個人會撞到限購 4 —— 而這條測的是
        # 漂移偵測,不該因為限購而變紅。
        assert await reserve_seats_and_enqueue(
            redis, event_id=seated["event_id"], zone_id=seated["zone_id"],
            user_id=seated["user_id"] + buyer, quantity=2, total_price_cents=2000,
            idempotency_key=str(uuid4()),
        ) is not None
    # Redis 已扣但 DB 還沒落帳(stream 有 backlog)→ 補集檢查必須跳過而不是誤報。
    assert await _drift(redis) == []


# ─ 三條不變式各自被破壞

async def test_broken_reverse_index_is_detected(redis, seated) -> None:
    """ends 壞掉時 runs 還看起來正常 —— 要等下一次合併才會把兩個 run 併成宣稱
    同一批座位的一個。所以這條是最早的訊號,而且它不依賴 DB。"""
    event_id, zone_id = seated["event_id"], seated["zone_id"]
    await redis.hset(_ends_key(event_id, zone_id), "999:5", "0")

    drifts = await _drift(redis)
    assert {"kind": "index", "event_id": event_id, "zone_id": zone_id} in drifts


async def test_a_missing_reverse_index_entry_is_detected(redis, seated) -> None:
    event_id, zone_id = seated["event_id"], seated["zone_id"]
    field = next(iter(await redis.hgetall(_ends_key(event_id, zone_id))))
    await redis.hdel(_ends_key(event_id, zone_id), field)

    assert any(d["kind"] == "index" for d in await _drift(redis))


async def test_counter_drift_is_detected(redis, seated) -> None:
    event_id, zone_id = seated["event_id"], seated["zone_id"]
    await redis.set(_zone_available_key(event_id, zone_id), 42)

    drifts = await _drift(redis)
    assert {
        "kind": "counter", "event_id": event_id, "zone_id": zone_id,
        "expected": 12, "actual": 42,
    } in drifts


async def test_structure_that_is_not_the_db_complement_is_detected(
    db, redis, seated
) -> None:
    """DB 說某段被佔用,Redis 卻說它是空的 —— 那些座位會被賣第二次。"""
    event_id, zone_id = seated["event_id"], seated["zone_id"]
    block_id = await db.scalar(
        select(SeatBlock.id).where(SeatBlock.zone_id == zone_id)
    )
    order = Order(
        user_id=seated["user_id"], event_id=event_id, zone_id=zone_id,
        quantity=4, total_price_cents=4000, status=OrderStatus.PENDING,
        idempotency_key=uuid4(),
    )
    db.add(order)
    await db.flush()
    db.add(SeatHold(
        event_id=event_id, block_id=block_id, order_id=order.id,
        start_pos=4, length=4,
    ))
    await db.commit()
    # Redis 沒跟著改 —— 它仍然認為整個 block 是空的。

    drifts = await _drift(redis)
    assert {"kind": "complement", "event_id": event_id, "zone_id": zone_id} in drifts


# ─ backlog 對三條檢查的影響不同

async def test_redis_internal_checks_run_even_with_a_backlog(redis, seated) -> None:
    """index 與 counter 是純 Redis 的不變式(同一支 Lua 裡一起改的),所以 stream
    有 backlog 也照查 —— 它們是最早的訊號,不該被 backlog 遮住。"""
    event_id, zone_id = seated["event_id"], seated["zone_id"]
    assert await reserve_seats_and_enqueue(          # 製造 backlog
        redis, event_id=event_id, zone_id=zone_id, user_id=seated["user_id"],
        quantity=2, total_price_cents=2000, idempotency_key=str(uuid4()),
    ) is not None
    await redis.set(_zone_available_key(event_id, zone_id), 999)

    drifts = await _drift(redis)
    assert any(d["kind"] == "counter" for d in drifts)
    assert not any(d["kind"] == "complement" for d in drifts), "補集檢查應該被跳過"


async def test_unseated_events_are_not_inspected(redis, published_event) -> None:
    """純計數器的舊場次沒有座位結構可查 —— 它由 detect_inventory_drift 顧。"""
    assert await _drift(redis) == []


# ─ A4:event 計數器必須等於各 zone 計數器之和

async def test_event_total_must_equal_the_sum_of_zone_counters(redis, seated) -> None:
    """座位訂單的每次配位都同時扣 zone 與 event 兩個計數器,所以這條必須成立。

    它抓的是「`reconcile_inventory` 只修了 event 計數器」之後留下的不一致 ——
    那種不一致三條 per-zone 檢查都看不到(`counter` 只比對單一 zone 內部)。
    """
    from app.services.inventory import _key as _event_available_key

    event_id, zone_id = seated["event_id"], seated["zone_id"]
    assert await _drift(redis) == []

    await redis.set(_event_available_key(event_id), 99)
    drifts = await _drift(redis)
    assert {
        "kind": "event_total", "event_id": event_id, "expected": 12, "actual": 99,
    } in drifts


# ─ C13:只檢查「key 還應該存在」的場次

async def test_long_finished_events_are_not_inspected(db, redis, seated) -> None:
    """少了這個過濾,兩年前結束的場次會永遠每 5 分鐘被檢查一次 —— 成本隨歷史無界。

    檢查窗口刻意跟 purge_finished_event_keys 的保留期一致:我們只在 key 還應該
    存在的期間內檢查它。
    """
    from datetime import timedelta

    from app.worker import EVENT_KEY_RETENTION_DAYS

    event_id, zone_id = seated["event_id"], seated["zone_id"]
    await redis.set(_zone_available_key(event_id, zone_id), 42)   # 故意弄壞
    assert any(d["kind"] == "counter" for d in await _drift(redis))

    event = await db.get(Event, event_id)
    event.ends_at = datetime.now(timezone.utc) - timedelta(
        days=EVENT_KEY_RETENTION_DAYS + 1
    )
    event.starts_at = event.ends_at - timedelta(hours=2)
    await db.commit()
    assert await _drift(redis) == [], "已過保留期的場次不該再被檢查"
