"""每人每場次的限購。

這個功能唯一難的地方是**併發**:黃牛不是「買了四張再多按一次」,而是同時送出二十筆
請求。任何「先查再扣」的實作在那種情況下二十筆都會讀到同一個舊值而全部放行 ——
所以檢查必須跟扣庫存在同一支 Lua 裡,而這個檔案最重要的兩條測試就是併發那兩條。

第二難的是**退額度**。限購含 PENDING(不然併發就擋不住),於是每一條釋放路徑
(取消、過期、dead-letter)都必須退回額度。漏掉任何一條的症狀是「下單失敗過的人
再也買不了這場」,而且不會有任何錯誤訊息 —— 要等到客訴才會被發現。
"""
import asyncio
from datetime import datetime, timedelta, timezone
from uuid import uuid4

import pytest
import pytest_asyncio
from sqlalchemy import select

from app.core.config import get_settings, max_purchasable
from app.core.exceptions import PurchaseLimitExceeded
from app.core.security import create_admission_token
from app.models.event import Event, EventStatus
from app.models.order import Order, OrderStatus
from app.models.seating import EventZonePrice, Zone
from app.models.user import User
from app.scripts.seed_venue import RowSpec, VenueSpec, ZoneSpec, seed_venue
from app.services.inventory import (
    ReserveOutcome,
    _purchased_key,
    release,
    reserve_and_enqueue,
)
from app.services.orders import release_order_seat
from app.services.publish_event import publish_event
from app.services.seat_runs import release_seats, reserve_seats_and_enqueue

LIMIT = get_settings().MAX_TICKETS_PER_USER_PER_EVENT


async def _held(redis, event_id: int, user_id: int) -> int:
    raw = await redis.hget(_purchased_key(event_id), str(user_id))
    return int(raw) if raw is not None else 0


# ─ 無座位圖的路徑

@pytest.mark.asyncio
async def test_a_buyer_can_reach_the_limit_across_several_orders(
    redis, published_event
) -> None:
    """限購是**累計**的,不是單筆的。一次一張買到上限必須剛好停在上限。"""
    for n in range(1, LIMIT + 1):
        result = await reserve_and_enqueue(
            redis, event_id=published_event.id, user_id=7, quantity=1,
            total_price_cents=100, idempotency_key=str(uuid4()),
        )
        assert result.outcome == ReserveOutcome.OK, f"第 {n} 張就被擋了"
        assert await _held(redis, published_event.id, 7) == n


@pytest.mark.asyncio
async def test_the_ticket_past_the_limit_is_refused(redis, published_event) -> None:
    for _ in range(LIMIT):
        await reserve_and_enqueue(
            redis, event_id=published_event.id, user_id=7, quantity=1,
            total_price_cents=100, idempotency_key=str(uuid4()),
        )
    result = await reserve_and_enqueue(
        redis, event_id=published_event.id, user_id=7, quantity=1,
        total_price_cents=100, idempotency_key=str(uuid4()),
    )
    assert result.outcome == ReserveOutcome.OVER_LIMIT
    assert result.held == LIMIT and result.limit == LIMIT
    assert await _held(redis, published_event.id, 7) == LIMIT, "被拒的請求不能加額度"


@pytest.mark.asyncio
async def test_a_rejected_request_does_not_consume_stock(
    redis, published_event
) -> None:
    """被限購擋下的請求絕不能扣庫存 —— 否則黃牛用註冊帳號狂送就能把票鎖死。"""
    from app.services.inventory import get_available

    await reserve_and_enqueue(
        redis, event_id=published_event.id, user_id=7, quantity=LIMIT,
        total_price_cents=100, idempotency_key=str(uuid4()),
    )
    before = await get_available(redis, event_id=published_event.id)
    for _ in range(5):
        await reserve_and_enqueue(
            redis, event_id=published_event.id, user_id=7, quantity=1,
            total_price_cents=100, idempotency_key=str(uuid4()),
        )
    assert await get_available(redis, event_id=published_event.id) == before


@pytest.mark.asyncio
async def test_sold_out_wins_over_the_limit(redis, published_event) -> None:
    """只剩 5 席卻要 6 張:回「售完」而不是「超過限購」。

    順序是有意義的。回 OVER_LIMIT 會讓使用者以為減少張數就買得到,但票根本不夠;
    回 SOLD_OUT 並帶著 available 才是可行動的資訊。反過來(有票但這個人買滿了)
    OVER_LIMIT 才是唯一正確的原因。
    """
    result = await reserve_and_enqueue(
        redis, event_id=published_event.id, user_id=7, quantity=6,
        total_price_cents=100, idempotency_key=str(uuid4()),
    )
    assert result.outcome == ReserveOutcome.SOLD_OUT
    assert result.available == 5


@pytest.mark.asyncio
async def test_concurrent_requests_from_one_buyer_cannot_beat_the_limit(
    redis, published_event
) -> None:
    """**這個功能存在的理由。**

    黃牛不是買滿之後再多按一次,而是同時送出二十筆。任何「先 HGET 檢查、再 HINCRBY」
    的實作在這裡都會讓二十筆全部讀到 0 而一起放行。檢查與遞增在同一支 Lua 裡才擋得住。
    """
    results = await asyncio.gather(*[
        reserve_and_enqueue(
            redis, event_id=published_event.id, user_id=7, quantity=1,
            total_price_cents=100, idempotency_key=str(uuid4()),
        )
        for _ in range(20)
    ])
    ok = [r for r in results if r.outcome == ReserveOutcome.OK]
    assert len(ok) == LIMIT, f"併發突破了限購:成交 {len(ok)} 張"
    assert await _held(redis, published_event.id, 7) == LIMIT


@pytest.mark.asyncio
async def test_the_limit_is_per_event(redis, db, published_event) -> None:
    """買了 A 藝人的票不該排擠 B 藝人 —— 鍵是 (event, user) 而不是 user。"""
    now = datetime.now(timezone.utc)
    other = Event(
        name="另一場", venue="X",
        starts_at=now + timedelta(days=2), ends_at=now + timedelta(days=2, hours=2),
        sale_starts_at=now - timedelta(hours=1), sale_ends_at=now + timedelta(hours=1),
        total_seats=50, price_cents=100, status=EventStatus.PUBLISHED,
    )
    db.add(other)
    await db.flush()
    await redis.set(f"event:{other.id}:available", 50)

    for _ in range(LIMIT):
        await reserve_and_enqueue(
            redis, event_id=published_event.id, user_id=7, quantity=1,
            total_price_cents=100, idempotency_key=str(uuid4()),
        )
    result = await reserve_and_enqueue(
        redis, event_id=other.id, user_id=7, quantity=1,
        total_price_cents=100, idempotency_key=str(uuid4()),
    )
    assert result.outcome == ReserveOutcome.OK


# ─ 退額度:每一條釋放路徑

@pytest.mark.asyncio
async def test_releasing_gives_the_quota_back(redis, published_event) -> None:
    await reserve_and_enqueue(
        redis, event_id=published_event.id, user_id=7, quantity=LIMIT,
        total_price_cents=100, idempotency_key=str(uuid4()),
    )
    assert await reserve_and_enqueue(
        redis, event_id=published_event.id, user_id=7, quantity=1,
        total_price_cents=100, idempotency_key=str(uuid4()),
    ) is not None

    assert await release(
        redis, event_id=published_event.id, user_id=7,
        quantity=LIMIT, marker="order:1",
    )
    assert await _held(redis, published_event.id, 7) == 0
    assert (await reserve_and_enqueue(
        redis, event_id=published_event.id, user_id=7, quantity=1,
        total_price_cents=100, idempotency_key=str(uuid4()),
    )).outcome == ReserveOutcome.OK, "退了額度就必須買得回來"


@pytest.mark.asyncio
async def test_a_replayed_release_does_not_refund_twice(
    redis, published_event
) -> None:
    """重放不能把額度退成負的 —— 那個人就能無限買。

    退額度掛在跟座位同一個 `released:{marker}` SETNX 底下,所以兩者一起「最多一次」。
    """
    await reserve_and_enqueue(
        redis, event_id=published_event.id, user_id=7, quantity=2,
        total_price_cents=100, idempotency_key=str(uuid4()),
    )
    marker = "order:99"
    assert await release(
        redis, event_id=published_event.id, user_id=7, quantity=2, marker=marker,
    ) is True
    assert await release(
        redis, event_id=published_event.id, user_id=7, quantity=2, marker=marker,
    ) is False
    assert await _held(redis, published_event.id, 7) == 0


@pytest.mark.asyncio
async def test_releasing_an_order_from_before_the_feature_floors_at_zero(
    redis, published_event
) -> None:
    """功能上線前建立的訂單從來沒有加過額度。直接 HINCRBY 會把欄位打成負數 ——
    那個人就憑空多出額度,而且是永久的。歸零即刪的地板擋掉這件事。"""
    assert await _held(redis, published_event.id, 7) == 0     # 從沒買過
    await release(
        redis, event_id=published_event.id, user_id=7, quantity=2, marker="old:1",
    )
    assert await _held(redis, published_event.id, 7) == 0
    assert not await redis.hexists(_purchased_key(published_event.id), "7")


@pytest.mark.asyncio
async def test_cancelling_an_order_frees_the_quota(
    client, db, redis, published_event, drain_orders
) -> None:
    """走完整條 API:下單 → worker 落帳 → 取消 → 額度回來。

    直接測 `release()` 不夠 —— 真正會出錯的是「取消路徑忘了把 user_id 傳下去」,
    而那只有從訂單物件走一遍才看得出來。
    """
    await client.post("/v1/users/", json={"username": "capper", "password": "secret123"})
    token = (await client.post(
        "/v1/auth/token", data={"username": "capper", "password": "secret123"}
    )).json()["access_token"]
    uid = await db.scalar(select(User.id).where(User.username == "capper"))
    auth = {"Authorization": f"Bearer {token}"}

    r = await client.post(
        "/v1/orders/",
        json={"event_id": published_event.id, "quantity": LIMIT},
        headers={
            **auth,
            "Idempotency-Key": str(uuid4()),
            "Admission-Token": create_admission_token(
                user_id=uid, event_id=published_event.id, ttl_seconds=120
            ),
        },
    )
    assert r.status_code == 202, r.text
    await drain_orders()
    assert await _held(redis, published_event.id, uid) == LIMIT

    order_id = (await client.get("/v1/orders/me", headers=auth)).json()["items"][0]["id"]
    cancelled = await client.post(f"/v1/orders/{order_id}/cancel", headers=auth)
    assert cancelled.status_code == 204, cancelled.text
    assert await _held(redis, published_event.id, uid) == 0


async def _login(client, db, username: str) -> tuple[dict[str, str], int]:
    await client.post("/v1/users/", json={"username": username, "password": "secret123"})
    token = (await client.post(
        "/v1/auth/token", data={"username": username, "password": "secret123"}
    )).json()["access_token"]
    uid = await db.scalar(select(User.id).where(User.username == username))
    return {"Authorization": f"Bearer {token}"}, uid


@pytest.mark.asyncio
async def test_the_api_says_how_many_are_left_and_refunds_the_token(
    client, db, redis, published_event
) -> None:
    """409 要帶著 remaining,而且入場券必須退還。

    不變式是「入場券被消耗 ⟺ 訂單意圖已受理」。限購的拒絕發生在任何寫入之前
    (兩支 Lua 都在扣庫存前就回 OVER_LIMIT),所以退還是安全的 —— 而且是必要的:
    使用者只要改小張數就買得到,不該被迫回去重新排隊。

    先買 LIMIT-1 張再要 2 張,是因為 `quantity` 的上限已經被夾成 LIMIT ——
    「一次就要超過上限」現在在驗證層就被擋掉(422),走不到這條路徑。真正還會發生的
    是這個:分批買,最後一筆跨過線。而且它讓 remaining 是有資訊量的 1 而不是 LIMIT。
    """
    auth, uid = await _login(client, db, "greedy")
    common = {
        **auth,
        "Admission-Token": create_admission_token(
            user_id=uid, event_id=published_event.id, ttl_seconds=120
        ),
    }
    first = await client.post(
        "/v1/orders/",
        json={"event_id": published_event.id, "quantity": LIMIT - 1},
        headers={
            **auth,
            "Admission-Token": create_admission_token(
                user_id=uid, event_id=published_event.id, ttl_seconds=120
            ),
            "Idempotency-Key": str(uuid4()),
        },
    )
    assert first.status_code == 202, first.text

    rejected = await client.post(          # 3 + 2 > 4
        "/v1/orders/",
        json={"event_id": published_event.id, "quantity": 2},
        headers={**common, "Idempotency-Key": str(uuid4())},
    )
    assert rejected.status_code == 409, rejected.text
    body = rejected.json()
    assert body["limit"] == LIMIT
    assert body["already_held"] == LIMIT - 1
    assert body["remaining"] == 1, "要告訴使用者還能買幾張,不是只說被擋了"

    retry = await client.post(          # 同一張入場券,改成剩下的 1 張
        "/v1/orders/",
        json={"event_id": published_event.id, "quantity": 1},
        headers={**common, "Idempotency-Key": str(uuid4())},
    )
    assert retry.status_code == 202, retry.text


@pytest.mark.asyncio
async def test_asking_for_more_than_the_limit_is_rejected_at_the_boundary(
    client, db, redis, published_event
) -> None:
    """一次就要超過上限 → 422,而且連入場券都不會被碰。

    `quantity` 的 `le=` 是 min(單筆上限, 每人限購)。不夾的話這種請求會通過驗證、
    驗完入場券、跑進 Redis,最後才拿到 409 —— 而 OpenAPI 仍然對外宣告可以買 10 張,
    前端的數量選單就照著生出四個永遠買不到的選項。
    """
    auth, uid = await _login(client, db, "overreach")
    admission = create_admission_token(
        user_id=uid, event_id=published_event.id, ttl_seconds=120
    )
    common = {**auth, "Admission-Token": admission}

    r = await client.post(
        "/v1/orders/",
        json={"event_id": published_event.id, "quantity": LIMIT + 1},
        headers={**common, "Idempotency-Key": str(uuid4())},
    )
    assert r.status_code == 422, r.text
    assert await _held(redis, published_event.id, uid) == 0

    ok = await client.post(          # 同一張入場券仍然有效
        "/v1/orders/",
        json={"event_id": published_event.id, "quantity": LIMIT},
        headers={**common, "Idempotency-Key": str(uuid4())},
    )
    assert ok.status_code == 202, ok.text


def test_the_advertised_maximum_matches_the_enforced_one() -> None:
    """OpenAPI 宣告的上限必須等於實際執行的上限。

    這條擋的是「有人把 `le=` 改回寫死的數字」:文件說 10、實際擋 4,前端照文件
    生出來的選單有四成的選項是死的,而且沒有任何測試會紅。
    """
    from app.schemas.order import OrderCreate

    advertised = OrderCreate.model_json_schema()["properties"]["quantity"]["maximum"]
    assert advertised == max_purchasable() == LIMIT


# ─ 座位場次走的是另一支 Lua,所以要各測一次

@pytest_asyncio.fixture
async def seated(db, redis):
    spec = VenueSpec(
        name="Limit Arena",
        zones=(ZoneSpec(name="L區", display_order=0, rows=(RowSpec("A", (12, 12)),)),),
    )
    venue = await seed_venue(db, spec)
    zone_id = await db.scalar(select(Zone.id).where(Zone.venue_id == venue.id))
    now = datetime.now(timezone.utc)
    event = Event(
        name="Limit Show", venue=spec.name, venue_id=venue.id,
        starts_at=now + timedelta(days=1), ends_at=now + timedelta(days=1, hours=2),
        sale_starts_at=now - timedelta(hours=1), sale_ends_at=now + timedelta(hours=1),
        total_seats=24, price_cents=100, status=EventStatus.DRAFT,
    )
    db.add(event)
    await db.flush()
    db.add(EventZonePrice(event_id=event.id, zone_id=zone_id, price_cents=1000))
    await publish_event(db, redis, event)
    await db.commit()
    return event.id, zone_id


@pytest.mark.asyncio
async def test_the_seated_path_enforces_the_same_limit(redis, seated) -> None:
    event_id, zone_id = seated
    assert await reserve_seats_and_enqueue(
        redis, event_id=event_id, zone_id=zone_id, user_id=7, quantity=LIMIT,
        total_price_cents=400, idempotency_key=str(uuid4()),
    ) is not None

    with pytest.raises(PurchaseLimitExceeded) as excinfo:
        await reserve_seats_and_enqueue(
            redis, event_id=event_id, zone_id=zone_id, user_id=7, quantity=2,
            total_price_cents=200, idempotency_key=str(uuid4()),
        )
    assert excinfo.value.remaining == 0
    assert excinfo.value.limit == LIMIT


@pytest.mark.asyncio
async def test_the_seated_limit_is_not_reported_as_contention(redis, seated) -> None:
    """超過限購**不能**走成 SeatContention(503)。

    503 + Retry-After 的語意是「暫時性,原樣重送就會成功」,客戶端與 SDK 會照著自動
    重試 —— 但這個拒絕重送一萬次都一樣。如果 OVER_LIMIT 在 CAS 迴圈裡被當成
    `continue`,結果就正是那個:撞滿五次重試然後回 503。
    """
    from app.core.exceptions import SeatContention

    event_id, zone_id = seated
    await reserve_seats_and_enqueue(
        redis, event_id=event_id, zone_id=zone_id, user_id=7, quantity=LIMIT,
        total_price_cents=400, idempotency_key=str(uuid4()),
    )
    with pytest.raises(PurchaseLimitExceeded):
        await reserve_seats_and_enqueue(
            redis, event_id=event_id, zone_id=zone_id, user_id=7, quantity=1,
            total_price_cents=100, idempotency_key=str(uuid4()),
        )
    assert not issubclass(PurchaseLimitExceeded, SeatContention)


@pytest.mark.asyncio
async def test_releasing_seats_gives_the_quota_back(redis, seated) -> None:
    event_id, zone_id = seated
    reserved = await reserve_seats_and_enqueue(
        redis, event_id=event_id, zone_id=zone_id, user_id=7, quantity=LIMIT,
        total_price_cents=400, idempotency_key=str(uuid4()),
    )
    assert reserved is not None
    assert await _held(redis, event_id, 7) == LIMIT

    assert await release_seats(
        redis, event_id=event_id, zone_id=zone_id, user_id=7,
        block_id=reserved.block_id, start_pos=reserved.start_pos,
        length=reserved.length, marker="order:5",
    )
    assert await _held(redis, event_id, 7) == 0
    assert await reserve_seats_and_enqueue(
        redis, event_id=event_id, zone_id=zone_id, user_id=7, quantity=2,
        total_price_cents=200, idempotency_key=str(uuid4()),
    ) is not None


@pytest.mark.asyncio
async def test_a_rejected_seat_release_does_not_refund_the_quota(
    redis, seated
) -> None:
    """釋放被驗證擋下時(區間本來就是空的)不能退額度。

    退了的話,一次拿錯區間的呼叫就替那個人憑空製造額度 —— 而那個呼叫是拋例外的,
    沒有人會把它當成「額度變了」。額度的遞減必須在寫 marker 之後,跟座位同命運。
    """
    from app.core.exceptions import SeatReleaseOverlap

    event_id, zone_id = seated
    reserved = await reserve_seats_and_enqueue(
        redis, event_id=event_id, zone_id=zone_id, user_id=7, quantity=2,
        total_price_cents=200, idempotency_key=str(uuid4()),
    )
    assert reserved is not None

    with pytest.raises(SeatReleaseOverlap):
        await release_seats(          # 一段本來就空的區間
            redis, event_id=event_id, zone_id=zone_id, user_id=7,
            block_id=reserved.block_id, start_pos=reserved.start_pos + 6,
            length=2, marker="bogus",
        )
    assert await _held(redis, event_id, 7) == 2, "被拒的釋放不能動到額度"
