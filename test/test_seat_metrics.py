"""座位配位指標的正確性。

這些指標不是「效能觀測」而是**架構決定的守衛**:配位刻意沒有移進 Lua,而那個決定
掛在 N = λ × T 上,T 是估的不是量的。指標壞掉 = 那個決定失去監督,而且不會有人發現。

所以每一條都要有測試證明它真的會動 —— 一個永遠是 0 的計數器跟「系統很健康」
長得一模一樣。
"""
from uuid import uuid4

import pytest
import pytest_asyncio
from datetime import datetime, timedelta, timezone
from prometheus_client import REGISTRY
from sqlalchemy import select

from app.core.exceptions import NoSeatsAvailable
from app.core.seat_metrics import SEAT_CAS_WINDOW
from app.models.event import Event, EventStatus
from app.models.seating import EventZonePrice, Zone
from app.scripts.seed_venue import RowSpec, VenueSpec, ZoneSpec, seed_venue
from app.services.publish_event import publish_event
from app.services.seat_runs import _runs_key, reserve_seats_and_enqueue



def _counter(name: str, **labels: str) -> float:
    """指標是 process 全域且會跨測試累加,所以只能量**差值**。"""
    return REGISTRY.get_sample_value(name, labels) or 0.0


def _window_count() -> float:
    return _counter("seat_cas_window_seconds_count")


def _window_sum() -> float:
    return _counter("seat_cas_window_seconds_sum")


@pytest_asyncio.fixture
async def zone(db, redis):
    spec = VenueSpec(
        name="Metrics Arena",
        zones=(ZoneSpec(name="M區", display_order=0, rows=(RowSpec("A", (12,)),)),),
    )
    venue = await seed_venue(db, spec)
    zone_id = await db.scalar(select(Zone.id).where(Zone.venue_id == venue.id))
    now = datetime.now(timezone.utc)
    event = Event(
        name="Metrics Show", venue=spec.name, venue_id=venue.id,
        starts_at=now + timedelta(days=1), ends_at=now + timedelta(days=1, hours=2),
        sale_starts_at=now - timedelta(hours=1), sale_ends_at=now + timedelta(hours=1),
        total_seats=12, price_cents=100, status=EventStatus.DRAFT,
    )
    db.add(event)
    await db.flush()
    db.add(EventZonePrice(event_id=event.id, zone_id=zone_id, price_cents=1000))
    await publish_event(db, redis, event)
    await db.commit()
    return event.id, zone_id


@pytest.mark.asyncio
async def test_a_successful_reservation_records_one_ok_attempt(redis, zone) -> None:
    event_id, zone_id = zone
    before_ok = _counter("seat_cas_attempts_total", outcome="ok")
    before_windows = _window_count()
    before_sum = _window_sum()

    assert await reserve_seats_and_enqueue(
        redis, event_id=event_id, zone_id=zone_id, user_id=1, quantity=2,
        total_price_cents=200, idempotency_key=str(uuid4()),
    ) is not None

    assert _counter("seat_cas_attempts_total", outcome="ok") == before_ok + 1
    assert _window_count() == before_windows + 1
    assert _window_sum() > before_sum, "時間窗必須是正值 —— 0 代表計時器沒接上"


@pytest.mark.asyncio
async def test_attempts_per_reservation_is_one_without_contention(redis, zone) -> None:
    """沒有競爭時每筆成交只花一次 CAS。這個直方圖就是模擬裡的「放大倍數」。"""
    event_id, zone_id = zone
    before = _counter("seat_cas_attempts_per_reservation_bucket", le="1.0")

    await reserve_seats_and_enqueue(
        redis, event_id=event_id, zone_id=zone_id, user_id=1, quantity=2,
        total_price_cents=200, idempotency_key=str(uuid4()),
    )
    assert _counter("seat_cas_attempts_per_reservation_bucket", le="1.0") == before + 1


@pytest.mark.asyncio
async def test_a_dup_is_counted_separately_from_ok(redis, zone) -> None:
    """重放不是成交,不該混進 ok —— 否則放大倍數會被稀釋成看起來很健康。"""
    event_id, zone_id = zone
    key = str(uuid4())
    await reserve_seats_and_enqueue(
        redis, event_id=event_id, zone_id=zone_id, user_id=1, quantity=2,
        total_price_cents=200, idempotency_key=key,
    )
    before_ok = _counter("seat_cas_attempts_total", outcome="ok")
    before_dup = _counter("seat_cas_attempts_total", outcome="dup")

    assert await reserve_seats_and_enqueue(
        redis, event_id=event_id, zone_id=zone_id, user_id=1, quantity=2,
        total_price_cents=200, idempotency_key=key,
    ) is None

    assert _counter("seat_cas_attempts_total", outcome="dup") == before_dup + 1
    assert _counter("seat_cas_attempts_total", outcome="ok") == before_ok


@pytest.mark.asyncio
async def test_no_fit_does_not_pollute_the_window_histogram(redis, zone) -> None:
    """5 席只剩一段時 4 張配不出來 —— 那個請求不該產生 T 的樣本。"""
    event_id, zone_id = zone
    # 先把 12 席吃到只剩 5 席的一段
    await reserve_seats_and_enqueue(
        redis, event_id=event_id, zone_id=zone_id, user_id=1, quantity=4,
        total_price_cents=400, idempotency_key=str(uuid4()),
    )
    await reserve_seats_and_enqueue(
        redis, event_id=event_id, zone_id=zone_id, user_id=1, quantity=3,
        total_price_cents=300, idempotency_key=str(uuid4()),
    )
    runs = await redis.hgetall(_runs_key(event_id, zone_id))
    assert 5 in [int(v) for v in runs.values()], "前提:要有一段長度 5 的空段"

    before = _window_count()
    with pytest.raises(NoSeatsAvailable):
        await reserve_seats_and_enqueue(
            redis, event_id=event_id, zone_id=zone_id, user_id=1, quantity=4,
            total_price_cents=400, idempotency_key=str(uuid4()),
        )
    assert _window_count() == before, "沒做 CAS 的請求不該進時間窗直方圖"


def test_window_buckets_cover_the_expected_range() -> None:
    """T 的假設是 ~1.1ms。Prometheus 的預設桶從 5ms 起跳 —— 整個有意義的範圍都會
    落進第一個桶,p99 永遠算不出來。這條擋的是「有人把 buckets 拿掉」。
    """
    edges = [
        float(s.labels["le"])
        for metric in SEAT_CAS_WINDOW.collect()
        for s in metric.samples
        if s.name.endswith("_bucket") and s.labels["le"] != "+Inf"
    ]
    assert min(edges) <= 0.001, "必須有次毫秒的桶"
    assert sum(1 for e in edges if e <= 0.005) >= 4, "1ms 附近要有足夠解析度"
