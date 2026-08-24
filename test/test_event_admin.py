"""建立場次的兩種形狀。

存在的理由是端到端煙霧測試發現的缺口:整條座位功能做完之後,**沒有任何 API 能建立
座位場次** —— `EventCreate` 沒有 `venue_id` 也沒有 zone 票價,維運只能寫 SQL。

最後一條測試是這個檔案的重點:透過 API 建立的座位場次必須能**直接發佈成功**。
少了它,`total_seats` 與 `zone_prices` 的規則散在 create 與 publish 兩處,而兩處
不一致的後果就是那個「場次永遠賣不完」的 bug。
"""
from datetime import datetime, timedelta, timezone

import pytest
import pytest_asyncio
from sqlalchemy import select

from app.models.event import EventStatus
from app.models.seating import EventZonePrice, Venue, Zone
from app.models.user import User
from app.scripts.seed_venue import RowSpec, VenueSpec, ZoneSpec, seed_venue

pytestmark = pytest.mark.asyncio


def _payload(**overrides) -> dict:
    now = datetime.now(timezone.utc)
    body = {
        "name": "測試場次",
        "venue": "Admin Arena",
        "starts_at": (now + timedelta(days=30)).isoformat(),
        "ends_at": (now + timedelta(days=30, hours=3)).isoformat(),
        "sale_starts_at": (now + timedelta(days=1)).isoformat(),
        "sale_ends_at": (now + timedelta(days=2)).isoformat(),
    }
    body.update(overrides)
    return body


@pytest_asyncio.fixture
async def admin(client, db):
    await client.post("/v1/users/", json={"username": "boss", "password": "secret123"})
    user = await db.scalar(select(User).where(User.username == "boss"))
    user.is_admin = True
    await db.commit()
    token = await client.post(
        "/v1/auth/token", data={"username": "boss", "password": "secret123"}
    )
    return {"Authorization": f"Bearer {token.json()['access_token']}"}


@pytest_asyncio.fixture
async def arena(db):
    """搖滾區 (6,12,6)=24 + 看台 (10)=10 → 容量 34。"""
    spec = VenueSpec(
        name="Admin Arena",
        zones=(
            ZoneSpec(name="搖滾區", display_order=0, rows=(RowSpec("A", (6, 12, 6)),)),
            ZoneSpec(name="看台", display_order=1, rows=(RowSpec("B", (10,)),)),
        ),
    )
    venue = await seed_venue(db, spec)
    zones = {
        name: zid
        for name, zid in (
            await db.execute(select(Zone.name, Zone.id).where(Zone.venue_id == venue.id))
        ).all()
    }
    await db.commit()
    return {"venue_id": venue.id, "zones": zones, "capacity": spec.total_seats}


# ─ 舊形狀（無座位圖）不變

async def test_unseated_event_still_takes_a_flat_price(client, admin) -> None:
    r = await client.post(
        "/v1/events/",
        json=_payload(price_cents=1500, total_seats=100),
        headers=admin,
    )
    assert r.status_code == 201, r.text
    body = r.json()
    assert body["venue_id"] is None
    assert body["total_seats"] == 100 and body["price_cents"] == 1500


@pytest.mark.parametrize(
    "overrides",
    [
        {},                                        # 兩個都沒給
        {"price_cents": 1500},                     # 缺 total_seats
        {"total_seats": 100},                      # 缺 price_cents
        {"price_cents": 1500, "total_seats": 100, "zone_prices": {1: 100}},
    ],
)
async def test_unseated_event_shape_is_enforced(client, admin, overrides) -> None:
    r = await client.post("/v1/events/", json=_payload(**overrides), headers=admin)
    assert r.status_code == 422, r.text


# ─ 座位場次

async def test_seated_event_derives_total_seats_from_the_seat_map(
    client, db, admin, arena
) -> None:
    """`total_seats` 是 Σ block 容量的衍生值。要求管理員手打再在 publish 時驗證
    (SeatMapMismatch),等於刻意製造一類必然會發生的設定錯誤。"""
    r = await client.post(
        "/v1/events/",
        json=_payload(
            venue_id=arena["venue_id"],
            zone_prices={
                str(arena["zones"]["搖滾區"]): 580000,
                str(arena["zones"]["看台"]): 180000,
            },
        ),
        headers=admin,
    )
    assert r.status_code == 201, r.text
    body = r.json()
    assert body["venue_id"] == arena["venue_id"]
    assert body["total_seats"] == arena["capacity"] == 34

    prices = dict(
        (
            await db.execute(
                select(EventZonePrice.zone_id, EventZonePrice.price_cents).where(
                    EventZonePrice.event_id == body["id"]
                )
            )
        ).all()
    )
    assert prices == {
        arena["zones"]["搖滾區"]: 580000,
        arena["zones"]["看台"]: 180000,
    }


@pytest.mark.parametrize(
    "extra",
    [
        {"total_seats": 34},        # 衍生值不該由呼叫端提供
        {"price_cents": 1500},      # 座位場次按區計價
    ],
)
async def test_seated_event_rejects_the_unseated_fields(
    client, admin, arena, extra
) -> None:
    r = await client.post(
        "/v1/events/",
        json=_payload(
            venue_id=arena["venue_id"],
            zone_prices={str(zid): 100 for zid in arena["zones"].values()},
            **extra,
        ),
        headers=admin,
    )
    assert r.status_code == 422, r.text


async def test_seated_event_without_zone_prices_is_rejected(
    client, admin, arena
) -> None:
    r = await client.post(
        "/v1/events/", json=_payload(venue_id=arena["venue_id"]), headers=admin
    )
    assert r.status_code == 422, r.text


async def test_an_unknown_venue_is_404(client, admin) -> None:
    r = await client.post(
        "/v1/events/",
        json=_payload(venue_id=999999, zone_prices={"1": 100}),
        headers=admin,
    )
    assert r.status_code == 404, r.text


async def test_zone_prices_must_cover_every_zone(client, admin, arena) -> None:
    """少一個 zone → 那區的容量算進 total_seats 卻永遠賣不掉,場次永遠賣不完、
    等候室的 sold_out 永不觸發,而漂移偵測因內部自洽不會叫。所以要在建立時就擋。"""
    r = await client.post(
        "/v1/events/",
        json=_payload(
            venue_id=arena["venue_id"],
            zone_prices={str(arena["zones"]["搖滾區"]): 580000},   # 少了看台
        ),
        headers=admin,
    )
    assert r.status_code == 422, r.text
    assert r.json()["missing_zone_ids"] == [arena["zones"]["看台"]]


async def test_a_zone_from_another_venue_is_rejected(client, db, admin, arena) -> None:
    """多一個(別場館的 zone)是**安全問題**:少了這道檢查,建立時就能把別場館的
    zone 綁進來,之後拿它的便宜票價下單。"""
    other = Venue(name="Other Arena")
    db.add(other)
    await db.flush()
    foreign = Zone(venue_id=other.id, name="便宜區", display_order=0)
    db.add(foreign)
    await db.commit()

    r = await client.post(
        "/v1/events/",
        json=_payload(
            venue_id=arena["venue_id"],
            zone_prices={
                **{str(zid): 580000 for zid in arena["zones"].values()},
                str(foreign.id): 1,
            },
        ),
        headers=admin,
    )
    assert r.status_code == 422, r.text
    assert r.json()["unknown_zone_ids"] == [foreign.id]


# ─ 這個檔案的重點

async def test_an_api_created_seated_event_publishes_without_touching_the_db(
    client, admin, arena
) -> None:
    """透過 API 建立的座位場次必須能**直接發佈成功**。

    煙霧測試暴露的缺口是「沒有 API 能建座位場次」;但真正的風險是 total_seats 與
    zone_prices 的規則散在 create 與 publish 兩處。這條測試把兩處綁在一起 ——
    任何一邊改了規則而另一邊沒跟上,它就會紅。
    """
    created = await client.post(
        "/v1/events/",
        json=_payload(
            venue_id=arena["venue_id"],
            zone_prices={str(zid): 100000 for zid in arena["zones"].values()},
        ),
        headers=admin,
    )
    assert created.status_code == 201, created.text
    event_id = created.json()["id"]

    published = await client.post(f"/v1/events/{event_id}/publish", headers=admin)
    assert published.status_code == 200, published.text
    assert published.json()["status"] == EventStatus.PUBLISHED.value

    zones = await client.get(f"/v1/events/{event_id}/zones")
    assert zones.status_code == 200
    listed = zones.json()
    assert [z["available"] for z in listed] == [24, 10]
    assert all(z["price_cents"] == 100000 for z in listed)
