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
    """前區 6 席 / 後區 5 席,兩區都定價(publish 現在要求每個 zone 都有票價)。

    另外在**發佈之後**塞一個未定價的 zone —— 那是「蓋了新看台區」的真實情境,
    也是 `list_zone_availability` 那道過濾唯一還會被觸發的路徑。
    """
    spec = VenueSpec(
        name="Zones Arena",
        zones=(
            ZoneSpec(name="前區", display_order=0, rows=(RowSpec("A", (6,)),)),
            ZoneSpec(name="後區", display_order=1, rows=(RowSpec("B", (5,)),)),
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
        total_seats=11, price_cents=100, status=EventStatus.DRAFT,
    )
    db.add(event)
    await db.flush()
    db.add_all([
        EventZonePrice(event_id=event.id, zone_id=zones["前區"], price_cents=6000),
        EventZonePrice(event_id=event.id, zone_id=zones["後區"], price_cents=2000),
    ])
    await publish_event(db, redis, event)

    # 發佈之後才蓋好的新區:對這個場次沒有票價,所以不可賣。
    late = Zone(venue_id=venue.id, name="未定價區", display_order=2)
    db.add(late)
    await db.flush()
    zones["未定價區"] = late.id
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
    """發佈後才新增的 zone 對這個場次沒有票價,不可賣,就別出現在選單上。

    publish 現在會擋下「發佈時就有未定價 zone」的情況(那會讓場次永遠賣不完),
    所以這道過濾剩下的用途就是這個 —— 場館後來擴建。
    """
    body = (await client.get(f"/v1/events/{arena['event_id']}/zones")).json()
    assert "未定價區" not in [z["name"] for z in body]


async def test_available_quantities_is_not_max_contiguous(client, arena) -> None:
    """後區只有一段 5 連號:4 張配不出來(會留孤兒),1/2/3/5 可以。

    回 max_contiguous=5 然後拒絕 4 張,正是客服災難的來源。
    """
    body = (await client.get(f"/v1/events/{arena['event_id']}/zones")).json()
    back = next(z for z in body if z["name"] == "後區")
    assert back["available"] == 5
    # 結構上 {1,2,3,5} 可行,但每人限購 4 把 5 夾掉了 —— 建議一個誰都買不到的張數,
    # 使用者選了之後只會再吃一個 409。純函式層的 {L} ∪ [1, L-2] 由
    # test_seating.py::test_feasible_quantities_is_not_max_contiguous 守著。
    assert back["available_quantities"] == [1, 2, 3]
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
    assert body["block_index"] == 0
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
    """無座位圖的場次:座號這個資源根本不存在 → 404,不是 409。

    409 隱含「重試會有結果」。一個「輪詢到 200 才顯示座號」的客戶端,對無座位圖的
    訂單會永遠輪詢下去 —— 這條測試以前的名稱說 404、斷言卻寫 409,是我當時沒想
    清楚的痕跡。
    """
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
    ).status_code == 404


# ─ C11:選區畫面的快取

async def test_zone_list_is_cached_briefly(client, db, arena, buyer, redis) -> None:
    """2 秒快取:開賣前後這個端點會被瘋狂刷新,而每次真實計算是「每 zone 讀
    runs+geom + 跑 feasible_quantities」。使用者感覺不到 2 秒,而「快照過時可接受」
    本來就是這條唯讀路徑的前提。
    """
    from app.services.zones import _zones_cache_key

    event_id, zone_id = arena["event_id"], arena["zones"]["前區"]
    first = (await client.get(f"/v1/events/{event_id}/zones")).json()
    assert await redis.exists(_zones_cache_key(event_id)), "第一次應該回填快取"

    accepted = await client.post(
        "/v1/orders/",
        json={"event_id": event_id, "quantity": 2, "zone_id": zone_id},
        headers=_order_headers(buyer, event_id),
    )
    assert accepted.status_code == 202
    # 快取還在有效期內 → 仍回舊值(刻意的取捨)
    assert (await client.get(f"/v1/events/{event_id}/zones")).json() == first

    await redis.delete(_zones_cache_key(event_id))     # 模擬快取到期
    fresh = (await client.get(f"/v1/events/{event_id}/zones")).json()
    assert next(z for z in fresh if z["name"] == "前區")["available"] == 4


async def test_zone_list_is_rate_limited(client, arena, rate_limiting) -> None:
    """無認證端點,而且每次 cache miss 都有實質計算成本 —— 必須限流。"""
    limit = rate_limiting.ZONES_LIST_LIMIT_PER_MINUTE
    event_id = arena["event_id"]
    for _ in range(limit):
        assert (await client.get(f"/v1/events/{event_id}/zones")).status_code == 200
    assert (await client.get(f"/v1/events/{event_id}/zones")).status_code == 429
