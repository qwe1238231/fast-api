"""座位訂單的端到端生命週期:下單 → 落帳 → 釋放。

這個檔案守的是**接線**,不是演算法:zone 分流有沒有走對、seat_holds 有沒有跟
order 同一個 txn 建出來、三條釋放路徑有沒有把區間還回 Redis(而不是只還數量)。
無座位圖的舊路徑另有 200 多個測試守著,這裡只驗座位那一邊。
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
from app.services.inventory import _key as _event_available_key
from app.services.publish_event import publish_event
from app.services.seat_runs import _runs_key, _zone_available_key
from app.services.seating import Run

pytestmark = pytest.mark.asyncio


async def _redis_runs(redis, event_id: int, zone_id: int) -> list[Run]:
    raw = await redis.hgetall(_runs_key(event_id, zone_id))
    runs = [
        Run(int(field.split(":")[0]), int(field.split(":")[1]), int(length))
        for field, length in raw.items()
    ]
    return sorted(runs, key=lambda r: (r.block_id, r.start))


async def _seated_event(db, redis, *, blocks: tuple[int, ...], price: int = 2000):
    spec = VenueSpec(
        name=f"Flow Arena {blocks}",
        zones=(ZoneSpec(name="唯一區", display_order=0, rows=(RowSpec("A", blocks),)),),
    )
    venue = await seed_venue(db, spec)
    zone_id = await db.scalar(select(Zone.id).where(Zone.venue_id == venue.id))
    now = datetime.now(timezone.utc)
    event = Event(
        name="Flow Show", venue=spec.name, venue_id=venue.id,
        starts_at=now + timedelta(days=1), ends_at=now + timedelta(days=1, hours=2),
        sale_starts_at=now - timedelta(hours=1), sale_ends_at=now + timedelta(hours=1),
        total_seats=sum(blocks), price_cents=100, status=EventStatus.DRAFT,
    )
    db.add(event)
    await db.flush()
    db.add(EventZonePrice(event_id=event.id, zone_id=zone_id, price_cents=price))
    await publish_event(db, redis, event)
    await db.commit()
    return event, zone_id


@pytest_asyncio.fixture
async def buyer(client, db):
    await client.post("/v1/users/", json={"username": "seatbuyer", "password": "secret123"})
    token = await client.post(
        "/v1/auth/token", data={"username": "seatbuyer", "password": "secret123"}
    )
    bearer = {"Authorization": f"Bearer {token.json()['access_token']}"}
    user_id = await db.scalar(select(User.id).where(User.username == "seatbuyer"))
    return bearer, user_id


async def _fresh_buyer(client, db, n: int) -> tuple[dict[str, str], int]:
    """第 n 個買家(註冊 + 登入)。模擬型測試用它避開每人限購 —— 真實情況本來就是
    很多不同的人在搶,而不是同一個人連下十幾筆。"""
    name = f"seatbuyer{n}"
    await client.post("/v1/users/", json={"username": name, "password": "secret123"})
    token = await client.post(
        "/v1/auth/token", data={"username": name, "password": "secret123"}
    )
    bearer = {"Authorization": f"Bearer {token.json()['access_token']}"}
    return bearer, await db.scalar(select(User.id).where(User.username == name))


def _headers(buyer, event_id: int) -> dict[str, str]:
    bearer, user_id = buyer
    return {
        **bearer,
        "Admission-Token": create_admission_token(
            user_id=user_id, event_id=event_id, ttl_seconds=120
        ),
        "Idempotency-Key": str(uuid4()),
    }


# ─ publish 必須初始化空段結構

async def test_publish_seeds_the_run_structure(db, redis) -> None:
    """少了這一步,配位會看到零個空段、每筆訂單都回「配不出來」—— 而那跟真的
    湊不出連號長得一模一樣,沒有人會發現只是忘了初始化。"""
    event, zone_id = await _seated_event(db, redis, blocks=(6, 12))
    assert await _redis_runs(redis, event.id, zone_id) == sorted(
        [Run(b, 0, cap) for b, cap in
         zip(sorted({r.block_id for r in await _redis_runs(redis, event.id, zone_id)}), (6, 12))],
        key=lambda r: (r.block_id, r.start),
    )
    assert int(await redis.get(_zone_available_key(event.id, zone_id))) == 18


# ─ 完整生命週期

async def test_order_then_cancel_returns_the_interval(
    client, db, redis, drain_orders, buyer
) -> None:
    """取消必須把**區間**還回去(不只是數量),而且 seat_holds 那一列要消失。"""
    event, zone_id = await _seated_event(db, redis, blocks=(12,))
    event_id = event.id          # rollback 會 expire ORM 物件,先固定下來
    before = await _redis_runs(redis, event_id, zone_id)

    accepted = await client.post(
        "/v1/orders/",
        json={"event_id": event_id, "quantity": 3, "zone_id": zone_id},
        headers=_headers(buyer, event_id),
    )
    assert accepted.status_code == 202, accepted.text
    await drain_orders()

    order = await db.scalar(select(Order).where(Order.event_id == event_id))
    assert order is not None and order.zone_id == zone_id
    hold = await db.scalar(select(SeatHold).where(SeatHold.order_id == order.id))
    assert hold is not None and hold.length == 3
    assert int(await redis.get(_zone_available_key(event_id, zone_id))) == 9
    assert int(await redis.get(_event_available_key(event_id))) == 9

    order_id = order.id
    bearer, _ = buyer
    cancelled = await client.post(f"/v1/orders/{order_id}/cancel", headers=bearer)
    assert cancelled.status_code == 204, cancelled.text

    # 取消是另一個 session 做的。rollback 結束本地交易才看得到;之後一律用
    # 欄位層級的 select,不要碰已 expire 的 ORM 物件(那會在非 greenlet 的
    # 情境觸發延遲載入)。
    await db.rollback()
    assert await db.scalar(
        select(SeatHold.id).where(SeatHold.order_id == order_id)
    ) is None
    assert await _redis_runs(redis, event_id, zone_id) == before, "區間必須合併回原樣"
    assert int(await redis.get(_zone_available_key(event_id, zone_id))) == 12
    assert int(await redis.get(_event_available_key(event_id))) == 12


async def test_expiry_sweep_returns_the_interval(db, redis, drain_orders, client, buyer) -> None:
    """worker 的過期掃描走的是同一條釋放路徑。"""
    from app.worker import expire_pending_orders

    event, zone_id = await _seated_event(db, redis, blocks=(12,))
    event_id = event.id          # rollback 會 expire ORM 物件,先固定下來
    before = await _redis_runs(redis, event_id, zone_id)

    accepted = await client.post(
        "/v1/orders/",
        json={"event_id": event_id, "quantity": 2, "zone_id": zone_id},
        headers=_headers(buyer, event_id),
    )
    assert accepted.status_code == 202
    await drain_orders()

    order = await db.scalar(select(Order).where(Order.event_id == event_id))
    assert order is not None
    order.created_at = datetime.now(timezone.utc) - timedelta(hours=1)   # 推過門檻
    await db.commit()

    order_id = order.id
    await expire_pending_orders({"redis_client": redis})

    await db.rollback()
    assert await db.scalar(
        select(Order.status).where(Order.id == order_id)
    ) == OrderStatus.EXPIRED
    assert await db.scalar(
        select(SeatHold.id).where(SeatHold.order_id == order_id)
    ) is None
    assert await _redis_runs(redis, event_id, zone_id) == before


async def test_dead_letter_returns_the_interval_when_never_persisted(
    db, redis, client, buyer
) -> None:
    """intent 從未落帳 → dead-letter 要還的是區間,不是數量。

    DB 沒有對應的 hold 列可刪(order 根本沒建出來),所以直接把區間還回 Redis。
    """
    from app.worker import _dead_letter_intent
    from app.services.inventory import ORDER_STREAM_KEY

    event, zone_id = await _seated_event(db, redis, blocks=(12,))
    before = await _redis_runs(redis, event.id, zone_id)

    accepted = await client.post(
        "/v1/orders/",
        json={"event_id": event.id, "quantity": 4, "zone_id": zone_id},
        headers=_headers(buyer, event.id),
    )
    assert accepted.status_code == 202
    entry_id, fields = (await redis.xrange(ORDER_STREAM_KEY))[0]

    await _dead_letter_intent(redis, entry_id, dict(fields))

    assert await _redis_runs(redis, event.id, zone_id) == before
    assert int(await redis.get(_zone_available_key(event.id, zone_id))) == 12


# ─ NO_FIT 一路走到 HTTP

async def test_no_fit_returns_409_with_the_feasible_quantities(
    client, db, redis, buyer
) -> None:
    """只剩一段 5 連號時 4 張配不出來(會留孤兒),但 1/2/3/5 可以。

    這必須跟售完分開:回「售完」而使用者看得到 5 個空位,是客服災難的來源。
    """
    event, zone_id = await _seated_event(db, redis, blocks=(5,))
    response = await client.post(
        "/v1/orders/",
        json={"event_id": event.id, "quantity": 4, "zone_id": zone_id},
        headers=_headers(buyer, event.id),
    )
    assert response.status_code == 409, response.text
    body = response.json()
    # 結構上 5 張配得出來(整段賣掉不留孤兒),但每人限購 4 把它夾掉了 —— 不能
    # 建議一個誰都買不到的張數。純函式層的 {L} ∪ [1, L-2] 由
    # test_seating.py::test_feasible_quantities_is_not_max_contiguous 守著。
    assert body["available_quantities"] == [1, 2, 3]
    assert body["requested"] == 4
    # 拒絕發生在扣庫存之前。
    assert int(await redis.get(_zone_available_key(event.id, zone_id))) == 5


async def test_no_fit_refunds_the_admission_token(client, db, redis, buyer) -> None:
    """NO_FIT 本質上可重試(改個張數就成立),所以入場券必須退還。

    這是 admission token 那個修正真正要服務的情境 —— 沒有它,使用者被告知「4 張
    配不出來、可以買 3 張」卻發現入場券已作廢,得回去重新排隊。
    """
    event, zone_id = await _seated_event(db, redis, blocks=(5,))
    bearer, user_id = buyer
    token = create_admission_token(user_id=user_id, event_id=event.id, ttl_seconds=120)
    common = {**bearer, "Admission-Token": token}

    rejected = await client.post(
        "/v1/orders/",
        json={"event_id": event.id, "quantity": 4, "zone_id": zone_id},
        headers={**common, "Idempotency-Key": str(uuid4())},
    )
    assert rejected.status_code == 409

    retry = await client.post(
        "/v1/orders/",
        json={"event_id": event.id, "quantity": 3, "zone_id": zone_id},
        headers={**common, "Idempotency-Key": str(uuid4())},
    )
    assert retry.status_code == 202, retry.text


async def test_group_of_two_always_sits_together(
    client, db, redis, drain_orders, buyer
) -> None:
    """把一個 zone 買到配不出來為止,每一筆的座位都必須是連續的一段。"""
    event, zone_id = await _seated_event(db, redis, blocks=(6, 12, 6))
    placed = 0
    while True:
        # 每筆換一個買家:每人限購 4 張,同一個人第三筆就會被擋 —— 而這條測的是
        # 「兩人票永遠坐在一起」,不是限購。真實情況本來就是很多不同的人在搶。
        response = await client.post(
            "/v1/orders/",
            json={"event_id": event.id, "quantity": 2, "zone_id": zone_id},
            headers=_headers(await _fresh_buyer(client, db, placed), event.id),
        )
        if response.status_code == 409:
            break
        assert response.status_code == 202, response.text
        placed += 1
        assert placed < 20, "沒有收斂 —— 庫存沒被扣?"

    await drain_orders()
    holds = (await db.scalars(select(SeatHold).where(SeatHold.event_id == event.id))).all()
    assert len(holds) == placed >= 10
    assert all(hold.length == 2 for hold in holds), "兩人票永遠是一段連續區間"
    # 24 席全部兩兩售罄,不該剩下任何孤兒。
    assert all(run.length != 1 for run in await _redis_runs(redis, event.id, zone_id))


# ─ 守衛：total_seats 必須等於座位圖容量

async def test_publish_refuses_a_mismatched_total_seats(db, redis) -> None:
    """填錯 total_seats 會讓 detect_inventory_drift 永久誤報(它用
    total_seats − SUM(quantity) 算期望值)—— 漂移偵測就變成狼來了。

    不自動修正而是拒絕發佈:庫存上限被悄悄改掉比發佈失敗嚴重得多。
    """
    from app.core.exceptions import SeatMapMismatch

    spec = VenueSpec(
        name="Mismatch Arena",
        zones=(ZoneSpec(name="唯一區", display_order=0, rows=(RowSpec("A", (12,)),)),),
    )
    venue = await seed_venue(db, spec)
    zone_id = await db.scalar(select(Zone.id).where(Zone.venue_id == venue.id))
    now = datetime.now(timezone.utc)
    event = Event(
        name="Mismatch Show", venue=spec.name, venue_id=venue.id,
        starts_at=now + timedelta(days=1), ends_at=now + timedelta(days=1, hours=2),
        sale_starts_at=now - timedelta(hours=1), sale_ends_at=now + timedelta(hours=1),
        total_seats=100,                      # 座位圖只有 12 席
        price_cents=100, status=EventStatus.DRAFT,
    )
    db.add(event)
    await db.flush()
    db.add(EventZonePrice(event_id=event.id, zone_id=zone_id, price_cents=1000))

    with pytest.raises(SeatMapMismatch) as excinfo:
        await publish_event(db, redis, event)
    assert excinfo.value.capacity == 12
    assert excinfo.value.total_seats == 100
    # 擋在改狀態之前 —— 失敗的發佈不該留下半途的狀態。
    assert event.status == EventStatus.DRAFT


async def test_publish_refuses_an_unpriced_zone(db, redis) -> None:
    """未定價的 zone 會讓場次**永遠賣不完**:它的容量算進 total_seats(上面那條
    檢查強制的)但下單會被拒,於是 event:available 的下限永遠 > 0、等候室的
    sold_out 永不觸發。而漂移偵測因為內部自洽也不會叫 —— 沒有警報的錯誤最貴。
    """
    from app.core.exceptions import ZonePricesIncomplete

    spec = VenueSpec(
        name="Unpriced Arena",
        zones=(
            ZoneSpec(name="有價區", display_order=0, rows=(RowSpec("A", (6,)),)),
            ZoneSpec(name="無價區", display_order=1, rows=(RowSpec("B", (6,)),)),
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
        name="Unpriced Show", venue=spec.name, venue_id=venue.id,
        starts_at=now + timedelta(days=1), ends_at=now + timedelta(days=1, hours=2),
        sale_starts_at=now - timedelta(hours=1), sale_ends_at=now + timedelta(hours=1),
        total_seats=12, price_cents=100, status=EventStatus.DRAFT,
    )
    db.add(event)
    await db.flush()
    db.add(EventZonePrice(event_id=event.id, zone_id=zones["有價區"], price_cents=1000))

    with pytest.raises(ZonePricesIncomplete) as excinfo:
        await publish_event(db, redis, event)
    assert excinfo.value.zone_ids == [zones["無價區"]]
    assert event.status == EventStatus.DRAFT, "擋在改狀態之前"
