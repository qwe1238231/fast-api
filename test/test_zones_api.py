"""選區與座號揭露的對外行為。

守的是兩條規則:
  1. 選區畫面回**可行張數集合**而不是「最大連號長度」—— 把約束編碼進前端的可選
     集合,使用者就不會送出註定失敗的請求。
  2. **確認之前絕不吐座號** —— 那是 pending hold 能被 compaction 滑動的唯一前提。
"""
from datetime import datetime, timedelta, timezone
from uuid import uuid4

import pytest
import pytest_asyncio
from sqlalchemy import select

from app.core.security import create_admission_token
from app.models.event import Event, EventStatus
from app.models.order import Order, OrderStatus
from app.models.seating import EventZonePrice, SeatHold, Zone
from app.models.user import User
from app.scripts.seed_venue import RowSpec, VenueSpec, ZoneSpec, seed_venue
from app.services.publish_event import publish_event

pytestmark = pytest.mark.asyncio


@pytest_asyncio.fixture
async def arena(db, redis):
    """前區 6 席 / 後區 5 席 / 未定價區 4 席。後區刻意是奇數以便測可行張數。"""
    spec = VenueSpec(
        name="Zones Arena",
        zones=(
            ZoneSpec(name="前區", display_order=0, rows=(RowSpec("A", (6,)),)),
            ZoneSpec(name="後區", display_order=1, rows=(RowSpec("B", (5,)),)),
            ZoneSpec(name="未定價區", display_order=2, rows=(RowSpec("C", (4,)),)),
        ),
    )
    venue = await seed_venue(db, spec)
    zones = {
        name: zid
        for name, zid in (
            await db.execute(select(Zone.name, Zone.id).where(Zone.venue_id == venue.id))
        ).all()
    }
    now = datetime.now(timezone.utc)
    event = Event(
        name="Zoned Show", venue=spec.name, venue_id=venue.id,
        starts_at=now + timedelta(days=1), ends_at=now + timedelta(days=1, hours=2),
        sale_starts_at=now - timedelta(hours=1), sale_ends_at=now + timedelta(hours=1),
        total_seats=15, price_cents=100, status=EventStatus.DRAFT,
    )
    db.add(event)
    await db.flush()
    db.add_all([
        EventZonePrice(event_id=event.id, zone_id=zones["前區"], price_cents=6000),
        EventZonePrice(event_id=event.id, zone_id=zones["後區"], price_cents=2000),
        # 「未定價區」刻意不設價 —— 不可賣,不該出現在選單上。
    ])
    await publish_event(db, redis, event)
    await db.commit()
    return {"event_id": event.id, "zones": zones}


@pytest_asyncio.fixture
async def buyer(client, db):
    await client.post("/v1/users/", json={"username": "zbuyer", "password": "secret123"})
    r = await client.post(
        "/v1/auth/token", data={"username": "zbuyer", "password": "secret123"}
    )
    bearer = {"Authorization": f"Bearer {r.json()['access_token']}"}
    uid = await db.scalar(select(User.id).where(User.username == "zbuyer"))
    return bearer, uid


def _order_headers(buyer, event_id: int) -> dict[str, str]:
    bearer, uid = buyer
    return {
        **bearer,
        "Admission-Token": create_admission_token(
            user_id=uid, event_id=event_id, ttl_seconds=120
        ),
        "Idempotency-Key": str(uuid4()),
    }


# ─ 選區

async def test_zone_list_is_ordered_and_priced(client, arena) -> None:
    response = await client.get(f"/v1/events/{arena['event_id']}/zones")
    assert response.status_code == 200, response.text
    body = response.json()

    assert [z["name"] for z in body] == ["前區", "後區"], "依 display_order,未定價區排除"
    assert [z["price_cents"] for z in body] == [6000, 2000]
    assert [z["available"] for z in body] == [6, 5]


async def test_unpriced_zone_is_not_offered(client, arena) -> None:
    """這場沒設票價的區不可賣,就別出現在選單上 —— 否則使用者選了只會拿到 422。"""
    body = (await client.get(f"/v1/events/{arena['event_id']}/zones")).json()
    assert "未定價區" not in [z["name"] for z in body]


async def test_available_quantities_is_not_max_contiguous(client, arena) -> None:
    """後區只有一段 5 連號:4 張配不出來(會留孤兒),1/2/3/5 可以。

    回 max_contiguous=5 然後拒絕 4 張,正是客服災難的來源。
    """
    body = (await client.get(f"/v1/events/{arena['event_id']}/zones")).json()
    back = next(z for z in body if z["name"] == "後區")
    assert back["available"] == 5
    assert back["available_quantities"] == [1, 2, 3, 5]
    assert 4 not in back["available_quantities"]


async def test_availability_drops_after_an_order(client, db, arena, buyer) -> None:
    event_id, zone_id = arena["event_id"], arena["zones"]["前區"]
    accepted = await client.post(
        "/v1/orders/",
        json={"event_id": event_id, "quantity": 2, "zone_id": zone_id},
        headers=_order_headers(buyer, event_id),
    )
    assert accepted.status_code == 202, accepted.text

    body = (await client.get(f"/v1/events/{event_id}/zones")).json()
    front = next(z for z in body if z["name"] == "前區")
    assert front["available"] == 4
    assert front["available_quantities"] == [1, 2, 4]   # 剩一段 4 連號 → 缺 3


async def test_unseated_event_has_no_zones(client, published_event) -> None:
    """舊的純計數器場次沒有區可選 —— 空清單是正確答案,不是錯誤。"""
    response = await client.get(f"/v1/events/{published_event.id}/zones")
    assert response.status_code == 200
    assert response.json() == []


async def test_missing_event_is_404(client) -> None:
    assert (await client.get("/v1/events/999999/zones")).status_code == 404


# ─ 座號揭露

async def test_seats_are_hidden_until_the_order_is_confirmed(
    client, db, arena, buyer, drain_orders
) -> None:
    event_id, zone_id = arena["event_id"], arena["zones"]["前區"]
    bearer, _ = buyer
    accepted = await client.post(
        "/v1/orders/",
        json={"event_id": event_id, "quantity": 2, "zone_id": zone_id},
        headers=_order_headers(buyer, event_id),
    )
    assert accepted.status_code == 202
    await drain_orders()
    order_id = await db.scalar(select(Order.id).where(Order.event_id == event_id))

    pending = await client.get(f"/v1/orders/{order_id}/seats", headers=bearer)
    assert pending.status_code == 409, "確認前不得吐座號"

    paid = await client.post(f"/v1/orders/{order_id}/pay", headers=bearer)
    assert paid.status_code == 204, paid.text

    revealed = await client.get(f"/v1/orders/{order_id}/seats", headers=bearer)
    assert revealed.status_code == 200, revealed.text
    body = revealed.json()
    assert body["zone_name"] == "前區"
    assert body["row_label"] == "A"
    assert len(body["labels"]) == 2


async def test_confirming_freezes_the_hold(
    client, db, arena, buyer, drain_orders
) -> None:
    """確認即凍結:confirmed_at 落下之後,compaction 就不該再搬這個 hold。

    凍結與狀態轉換同一個 txn,所以不可能出現「已確認但沒凍結」的 hold ——
    那種 hold 會讓 compaction 去搬一個使用者已經看過的座位。
    """
    event_id, zone_id = arena["event_id"], arena["zones"]["前區"]
    bearer, _ = buyer
    await client.post(
        "/v1/orders/",
        json={"event_id": event_id, "quantity": 2, "zone_id": zone_id},
        headers=_order_headers(buyer, event_id),
    )
    await drain_orders()
    order_id = await db.scalar(select(Order.id).where(Order.event_id == event_id))

    assert await db.scalar(
        select(SeatHold.confirmed_at).where(SeatHold.order_id == order_id)
    ) is None

    assert (await client.post(f"/v1/orders/{order_id}/pay", headers=bearer)).status_code == 204
    await db.rollback()
    assert await db.scalar(
        select(SeatHold.confirmed_at).where(SeatHold.order_id == order_id)
    ) is not None
    assert await db.scalar(
        select(Order.status).where(Order.id == order_id)
    ) == OrderStatus.CONFIRMED


async def test_seats_of_another_users_order_are_404(client, db, arena, buyer, drain_orders) -> None:
    event_id, zone_id = arena["event_id"], arena["zones"]["前區"]
    bearer, _ = buyer
    await client.post(
        "/v1/orders/",
        json={"event_id": event_id, "quantity": 2, "zone_id": zone_id},
        headers=_order_headers(buyer, event_id),
    )
    await drain_orders()
    order_id = await db.scalar(select(Order.id).where(Order.event_id == event_id))
    await client.post(f"/v1/orders/{order_id}/pay", headers=bearer)

    await client.post("/v1/users/", json={"username": "nosy", "password": "secret123"})
    other = await client.post(
        "/v1/auth/token", data={"username": "nosy", "password": "secret123"}
    )
    intruder = {"Authorization": f"Bearer {other.json()['access_token']}"}
    assert (
        await client.get(f"/v1/orders/{order_id}/seats", headers=intruder)
    ).status_code == 404


async def test_unseated_order_has_no_seats_resource(
    client, db, published_event, drain_orders
) -> None:
    """無座位圖的場次:座號這個資源根本不存在。"""
    await client.post("/v1/users/", json={"username": "flat", "password": "secret123"})
    r = await client.post(
        "/v1/auth/token", data={"username": "flat", "password": "secret123"}
    )
    bearer = {"Authorization": f"Bearer {r.json()['access_token']}"}
    uid = await db.scalar(select(User.id).where(User.username == "flat"))

    await client.post(
        "/v1/orders/",
        json={"event_id": published_event.id, "quantity": 1},
        headers={
            **bearer,
            "Admission-Token": create_admission_token(
                user_id=uid, event_id=published_event.id, ttl_seconds=120
            ),
            "Idempotency-Key": str(uuid4()),
        },
    )
    await drain_orders()
    order_id = await db.scalar(select(Order.id).where(Order.event_id == published_event.id))
    await client.post(f"/v1/orders/{order_id}/pay", headers=bearer)

    assert (
        await client.get(f"/v1/orders/{order_id}/seats", headers=bearer)
    ).status_code == 409
