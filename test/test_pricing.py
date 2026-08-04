"""分區票價的測試。

三層:純函式(零 I/O)、`load_zone_prices` 的白名單語意(**安全測試**)、
以及一條端到端 —— 從 API 經 pricing、Lua XADD、worker,一路到 orders 那一列。
"""
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from uuid import uuid4

import pytest
from sqlalchemy import select

from app.core.exceptions import ZoneNotForEvent, ZoneRequired
from app.core.security import create_admission_token
from app.models.event import Event, EventStatus
from app.models.order import Order
from app.models.seating import EventZonePrice, Venue, Zone
from app.models.user import User
from app.services.event_cache import get_event_meta, invalidate_event_meta
from app.services.inventory import set_initial_stock
from app.services.pricing import (
    load_zone_prices,
    order_total,
    price_for,
    total_for,
    unit_price,
)


@dataclass
class FakeMeta:
    """PricingContext 的最小實作 —— 純函式測試不需要 Redis 也不需要 DB。"""

    event_id: int = 1
    price_cents: int = 1500
    venue_id: int | None = None
    zone_prices: dict[int, int] = field(default_factory=dict)


# ─ 純函式:單價的五種情況

def test_unseated_event_uses_the_single_price() -> None:
    assert price_for(FakeMeta(), zone_id=None) == 1500


def test_unseated_event_rejects_a_zone() -> None:
    with pytest.raises(ZoneNotForEvent):
        price_for(FakeMeta(), zone_id=7)


def test_seated_event_requires_a_zone() -> None:
    with pytest.raises(ZoneRequired):
        price_for(FakeMeta(venue_id=3, zone_prices={7: 5000}), zone_id=None)


def test_seated_event_uses_the_zone_price() -> None:
    meta = FakeMeta(venue_id=3, zone_prices={7: 5000, 8: 2500})
    assert price_for(meta, zone_id=7) == 5000
    assert price_for(meta, zone_id=8) == 2500


def test_seated_event_never_falls_back_to_the_single_price() -> None:
    """缺某一區的票價是設定錯誤,寧可拒絕也不要按 fallback 價賣掉。

    fallback 只服務無座位圖的舊路徑。拿它去補座位場次的設定漏洞,結果會是把
    搖滾區按最低價賣出而且沒有任何錯誤 —— 那種營收 bug 要等對帳才會發現。
    """
    meta = FakeMeta(venue_id=3, price_cents=100, zone_prices={7: 5000})
    with pytest.raises(ZoneNotForEvent):
        price_for(meta, zone_id=8)


def test_order_total_multiplies_the_right_unit_price() -> None:
    meta = FakeMeta(venue_id=3, zone_prices={7: 5000})
    assert total_for(meta, zone_id=7, quantity=3) == 15000


@pytest.mark.parametrize("quantity", [0, -1])
def test_order_total_rejects_a_nonpositive_quantity(quantity: int) -> None:
    with pytest.raises(ValueError):
        order_total(
            event_id=1,
            venue_id=None,
            zone_prices={},
            fallback_price_cents=1500,
            zone_id=None,
            quantity=quantity,
        )


def test_unit_price_is_pure() -> None:
    """簽名裡沒有 db / redis —— 這是熱路徑不查 DB 的機械保證。"""
    import inspect

    params = set(inspect.signature(unit_price).parameters)
    assert not params & {"db", "redis", "session"}


# ─ load_zone_prices:白名單語意（安全測試）

@pytest.mark.asyncio
async def test_load_zone_prices_skips_zones_from_another_venue(db) -> None:
    """`event_zone_prices` 沒有任何約束阻止它指向別場館的 zone。

    FK 只各自指向 events 與 zones,沒有「zone 必須屬於 event 的 venue」這種
    跨表條件。所以 JOIN 過濾是唯一的防線:少了它,使用者帶另一個場館的便宜
    zone_id 就能買這場,而 webhook 的金額驗證抓不到 —— total_price_cents 是照
    那個便宜價算的,前後一致。
    """
    home, foreign = Venue(name="Home Arena"), Venue(name="Foreign Arena")
    db.add_all([home, foreign])
    await db.flush()

    home_zone = Zone(venue_id=home.id, name="搖滾區", display_order=0)
    foreign_zone = Zone(venue_id=foreign.id, name="便宜區", display_order=0)
    db.add_all([home_zone, foreign_zone])
    await db.flush()

    now = datetime.now(timezone.utc)
    event = Event(
        name="Home Show", venue="Home Arena", venue_id=home.id,
        starts_at=now + timedelta(days=1), ends_at=now + timedelta(days=1, hours=2),
        sale_starts_at=now - timedelta(hours=1), sale_ends_at=now + timedelta(hours=1),
        total_seats=10, price_cents=100, status=EventStatus.PUBLISHED,
    )
    db.add(event)
    await db.flush()

    db.add_all([
        EventZonePrice(event_id=event.id, zone_id=home_zone.id, price_cents=5000),
        EventZonePrice(event_id=event.id, zone_id=foreign_zone.id, price_cents=1),
    ])
    await db.commit()

    prices = await load_zone_prices(db, event_id=event.id, venue_id=home.id)
    assert prices == {home_zone.id: 5000}, "別場館的 zone 必須被濾掉"

    with pytest.raises(ZoneNotForEvent):
        unit_price(
            event_id=event.id, venue_id=home.id, zone_prices=prices,
            fallback_price_cents=100, zone_id=foreign_zone.id,
        )


@pytest.mark.asyncio
async def test_load_zone_prices_is_empty_for_an_unseated_event(db) -> None:
    assert await load_zone_prices(db, event_id=1, venue_id=None) == {}


# ─ cache:JSON 的字串 key 陷阱

@pytest.mark.asyncio
async def test_cached_zone_prices_keep_integer_keys(db, redis) -> None:
    """JSON 物件的 key 一定是字串。轉不回 int 的話每次查找都 miss,於是每一個
    zone 都變成「不可賣」—— 而且不會有任何錯誤訊息,只有 422。"""
    venue = Venue(name="Cache Arena")
    db.add(venue)
    await db.flush()
    zone = Zone(venue_id=venue.id, name="A區", display_order=0)
    db.add(zone)
    await db.flush()

    now = datetime.now(timezone.utc)
    event = Event(
        name="Cached Show", venue="Cache Arena", venue_id=venue.id,
        starts_at=now + timedelta(days=1), ends_at=now + timedelta(days=1, hours=2),
        sale_starts_at=now - timedelta(hours=1), sale_ends_at=now + timedelta(hours=1),
        total_seats=10, price_cents=100, status=EventStatus.PUBLISHED,
    )
    db.add(event)
    await db.flush()
    db.add(EventZonePrice(event_id=event.id, zone_id=zone.id, price_cents=7777))
    await db.commit()

    fresh = await get_event_meta(redis, db, event_id=event.id)      # miss → 回填
    cached = await get_event_meta(redis, db, event_id=event.id)     # hit
    assert fresh is not None and cached is not None
    assert fresh.zone_prices == cached.zone_prices == {zone.id: 7777}
    assert all(isinstance(key, int) for key in cached.zone_prices)
    assert cached.venue_id == venue.id
    assert price_for(cached, zone_id=zone.id) == 7777


# ─ 端到端:API → pricing → Lua → worker → DB

@pytest.mark.asyncio
async def test_seated_order_persists_zone_and_zone_price(
    client, db, redis, drain_orders
) -> None:
    """有座位圖的場次:金額按 zone 價、`orders.zone_id` 要真的落到 DB。"""
    venue = Venue(name="E2E Arena")
    db.add(venue)
    await db.flush()
    front = Zone(venue_id=venue.id, name="前區", display_order=0)
    back = Zone(venue_id=venue.id, name="後區", display_order=1)
    db.add_all([front, back])
    await db.flush()

    now = datetime.now(timezone.utc)
    event = Event(
        name="E2E Show", venue="E2E Arena", venue_id=venue.id,
        starts_at=now + timedelta(days=1), ends_at=now + timedelta(days=1, hours=2),
        sale_starts_at=now - timedelta(hours=1), sale_ends_at=now + timedelta(hours=1),
        total_seats=10, price_cents=100, status=EventStatus.PUBLISHED,
    )
    db.add(event)
    await db.flush()
    db.add_all([
        EventZonePrice(event_id=event.id, zone_id=front.id, price_cents=6000),
        EventZonePrice(event_id=event.id, zone_id=back.id, price_cents=2000),
    ])
    await db.commit()
    await set_initial_stock(redis, event_id=event.id, total_seats=event.total_seats)
    await invalidate_event_meta(redis, event_id=event.id)

    await client.post("/v1/users/", json={"username": "zoner", "password": "secret123"})
    token = await client.post(
        "/v1/auth/token", data={"username": "zoner", "password": "secret123"}
    )
    bearer = {"Authorization": f"Bearer {token.json()['access_token']}"}
    user_id = await db.scalar(select(User.id).where(User.username == "zoner"))

    def headers() -> dict[str, str]:
        return {
            **bearer,
            "Admission-Token": create_admission_token(
                user_id=user_id, event_id=event.id, ttl_seconds=120
            ),
            "Idempotency-Key": str(uuid4()),
        }

    # 不帶 zone → 422(座位場次必須指定區)
    missing = await client.post(
        "/v1/orders/",
        json={"event_id": event.id, "quantity": 2},
        headers=headers(),
    )
    assert missing.status_code == 422, missing.text

    # 帶後區 → 202,金額按後區價
    accepted = await client.post(
        "/v1/orders/",
        json={"event_id": event.id, "quantity": 2, "zone_id": back.id},
        headers=headers(),
    )
    assert accepted.status_code == 202, accepted.text

    await drain_orders()
    order = await db.scalar(select(Order).where(Order.event_id == event.id))
    assert order is not None
    assert order.zone_id == back.id, "zone_id 必須穿過 Lua 的 XADD 落到 DB"
    assert order.total_price_cents == 2 * 2000


@pytest.mark.asyncio
async def test_order_with_a_foreign_zone_is_rejected(client, db, redis) -> None:
    """帶別場館的 zone_id → 422,而且是在扣庫存之前就擋掉。"""
    home, foreign = Venue(name="Guard Home"), Venue(name="Guard Foreign")
    db.add_all([home, foreign])
    await db.flush()
    home_zone = Zone(venue_id=home.id, name="本區", display_order=0)
    foreign_zone = Zone(venue_id=foreign.id, name="他區", display_order=0)
    db.add_all([home_zone, foreign_zone])
    await db.flush()

    now = datetime.now(timezone.utc)
    event = Event(
        name="Guard Show", venue="Guard Home", venue_id=home.id,
        starts_at=now + timedelta(days=1), ends_at=now + timedelta(days=1, hours=2),
        sale_starts_at=now - timedelta(hours=1), sale_ends_at=now + timedelta(hours=1),
        total_seats=10, price_cents=100, status=EventStatus.PUBLISHED,
    )
    db.add(event)
    await db.flush()
    db.add_all([
        EventZonePrice(event_id=event.id, zone_id=home_zone.id, price_cents=6000),
        # 有人手動塞了一筆指向別場館 zone 的超便宜票價。
        EventZonePrice(event_id=event.id, zone_id=foreign_zone.id, price_cents=1),
    ])
    await db.commit()
    await set_initial_stock(redis, event_id=event.id, total_seats=event.total_seats)
    await invalidate_event_meta(redis, event_id=event.id)

    await client.post("/v1/users/", json={"username": "sneaky", "password": "secret123"})
    token = await client.post(
        "/v1/auth/token", data={"username": "sneaky", "password": "secret123"}
    )
    bearer = {"Authorization": f"Bearer {token.json()['access_token']}"}
    user_id = await db.scalar(select(User.id).where(User.username == "sneaky"))

    before = await redis.get(f"event:{event.id}:available")
    response = await client.post(
        "/v1/orders/",
        json={"event_id": event.id, "quantity": 2, "zone_id": foreign_zone.id},
        headers={
            **bearer,
            "Admission-Token": create_admission_token(
                user_id=user_id, event_id=event.id, ttl_seconds=120
            ),
            "Idempotency-Key": str(uuid4()),
        },
    )
    assert response.status_code == 422, response.text
    assert await redis.get(f"event:{event.id}:available") == before, "不該扣到庫存"
