import pytest
from uuid import uuid4

from sqlalchemy import select

from app.core.security import create_admission_token
from app.models.user import User
from app.services.inventory import get_available


async def auth_headers(client, username="alice"):
    await client.post("/v1/users/", json={"username": username, "password": "secret123"})
    r = await client.post(
        "/v1/auth/token", data={"username": username, "password": "secret123"}
    )
    return {"Authorization": f"Bearer {r.json()['access_token']}"}


async def _authed(client, db, username="alice"):
    """Create+login a user; return (bearer_headers, user_id)."""
    bearer = await auth_headers(client, username)
    user_id = await db.scalar(select(User.id).where(User.username == username))
    return bearer, user_id


def _buy_headers(bearer, user_id, event_id, *, idem=None):
    """Per-request order headers: auth + a FRESH single-use admission token + idempotency key.
    (A real client re-polls the queue for a fresh token per attempt.)"""
    return {
        **bearer,
        "Admission-Token": create_admission_token(user_id=user_id, event_id=event_id, ttl_seconds=120),
        "Idempotency-Key": idem or str(uuid4()),
    }


@pytest.mark.asyncio
async def test_create_order_succeeds(client, db, published_event, redis, drain_orders):
    bearer, uid = await _authed(client, db)

    r = await client.post(
        "/v1/orders/",
        json={"event_id": published_event.id, "quantity": 2},
        headers=_buy_headers(bearer, uid, published_event.id),
    )

    assert r.status_code == 202                                            # accepted, processing
    assert r.json()["status"] == "processing"
    assert await get_available(redis, event_id=published_event.id) == 3    # seat reserved at once (5 - 2)

    await drain_orders()                                                   # worker persists it
    orders = (await client.get("/v1/orders/me", headers=bearer)).json()["items"]
    assert len(orders) == 1
    assert orders[0]["status"] == "pending"
    assert orders[0]["quantity"] == 2


@pytest.mark.asyncio
async def test_sold_out_returns_409(client, db, published_event):
    # 5 席要兩個買家才吃得完 —— 每人限購 4。用一個人買 5 張的話,擋下來的會是
    # 限購而不是售完,這條測試就會為了完全不相干的理由變綠。
    bearer, uid = await _authed(client, db)
    other_bearer, other_uid = await _authed(client, db, username="bob")

    r1 = await client.post(                       # 4 張
        "/v1/orders/",
        json={"event_id": published_event.id, "quantity": 4},
        headers=_buy_headers(bearer, uid, published_event.id),
    )
    assert r1.status_code == 202
    r2 = await client.post(                       # 第 5 張 → 賣光
        "/v1/orders/",
        json={"event_id": published_event.id, "quantity": 1},
        headers=_buy_headers(other_bearer, other_uid, published_event.id),
    )
    assert r2.status_code == 202

    r3 = await client.post(                       # 再買 → 售完
        "/v1/orders/",
        json={"event_id": published_event.id, "quantity": 1},
        headers=_buy_headers(other_bearer, other_uid, published_event.id),
    )
    assert r3.status_code == 409


@pytest.mark.asyncio
async def test_idempotent_create_returns_same_order(client, db, published_event, redis, drain_orders):
    bearer, uid = await _authed(client, db)
    key = str(uuid4())
    body = {"event_id": published_event.id, "quantity": 1}

    r1 = await client.post("/v1/orders/", json=body, headers=_buy_headers(bearer, uid, published_event.id, idem=key))
    r2 = await client.post("/v1/orders/", json=body, headers=_buy_headers(bearer, uid, published_event.id, idem=key))

    assert r1.status_code == 202 and r2.status_code == 202
    assert r1.json()["idempotency_key"] == r2.json()["idempotency_key"]    # 同一個 handle
    assert await get_available(redis, event_id=published_event.id) == 4    # 只扣一次(5-1)

    await drain_orders()
    orders = (await client.get("/v1/orders/me", headers=bearer)).json()["items"]
    assert len(orders) == 1                                                # 只建了一筆,沒重複


@pytest.mark.asyncio
async def test_orders_me_keyset_pagination(client, db, published_event, drain_orders):
    bearer, uid = await _authed(client, db)
    for _ in range(3):                                                     # 建 3 筆訂單
        r = await client.post(
            "/v1/orders/",
            json={"event_id": published_event.id, "quantity": 1},
            headers=_buy_headers(bearer, uid, published_event.id),
        )
        assert r.status_code == 202
    await drain_orders()

    page1 = (await client.get("/v1/orders/me?limit=2", headers=bearer)).json()
    assert len(page1["items"]) == 2
    assert page1["next_cursor"] is not None                                # 還有下一頁

    page2 = (
        await client.get(f"/v1/orders/me?limit=2&cursor={page1['next_cursor']}", headers=bearer)
    ).json()
    assert len(page2["items"]) == 1
    assert page2["next_cursor"] is None                                    # 最後一頁

    ids = [o["id"] for o in page1["items"]] + [o["id"] for o in page2["items"]]
    assert len(set(ids)) == 3                                              # 不重疊、不遺漏
    assert ids == sorted(ids, reverse=True)                                # 跨頁維持新→舊

    bad = await client.get("/v1/orders/me?cursor=not-a-valid-cursor", headers=bearer)
    assert bad.status_code == 422                                          # 壞游標被擋


# --- admission enforcement -------------------------------------------------

@pytest.mark.asyncio
async def test_order_requires_admission_token(client, db, published_event):
    bearer, _ = await _authed(client, db)
    r = await client.post(
        "/v1/orders/",
        json={"event_id": published_event.id, "quantity": 1},
        headers={**bearer, "Idempotency-Key": str(uuid4())},   # no Admission-Token
    )
    assert r.status_code == 422                                # required header missing


@pytest.mark.asyncio
async def test_order_rejects_token_for_wrong_event(client, db, published_event):
    bearer, uid = await _authed(client, db)
    token = create_admission_token(user_id=uid, event_id=published_event.id + 999, ttl_seconds=120)
    r = await client.post(
        "/v1/orders/",
        json={"event_id": published_event.id, "quantity": 1},
        headers={**bearer, "Admission-Token": token, "Idempotency-Key": str(uuid4())},
    )
    assert r.status_code == 403                                # token scoped to another event


@pytest.mark.asyncio
async def test_order_rejects_replayed_token(client, db, published_event):
    bearer, uid = await _authed(client, db)
    token = create_admission_token(user_id=uid, event_id=published_event.id, ttl_seconds=120)
    common = {**bearer, "Admission-Token": token}

    r1 = await client.post("/v1/orders/", json={"event_id": published_event.id, "quantity": 1},
                           headers={**common, "Idempotency-Key": str(uuid4())})
    r2 = await client.post("/v1/orders/", json={"event_id": published_event.id, "quantity": 1},
                           headers={**common, "Idempotency-Key": str(uuid4())})
    assert r1.status_code == 202
    assert r2.status_code == 403                               # single-use: same token reused


# --- admission token 的退還 ----------------------------------------------
# SETNX 必須在扣庫存之前(否則同一張 token 的兩個並發請求都會通過,一票變兩票),
# 所以下單失敗會連帶燒掉入場券。售完是終局的所以以前看不出問題,但「配不出這個
# 張數」本質上可重試,而在讀-算-CAS 的配位架構下重試是常態路徑。不變式必須是
# 「入場券被消耗 ⟺ 訂單意圖已受理」。

@pytest.mark.asyncio
async def test_sold_out_refunds_the_admission_token(client, db, published_event, redis):
    """庫存不足被拒之後,同一張入場券必須還能改張數再試 —— 不必重新排隊。"""
    bearer, uid = await _authed(client, db)
    token = create_admission_token(user_id=uid, event_id=published_event.id, ttl_seconds=120)
    common = {**bearer, "Admission-Token": token}

    other_bearer, other_uid = await _authed(client, db, username="bob")
    await client.post(                                          # 5 → 1 席
        "/v1/orders/",
        json={"event_id": published_event.id, "quantity": 4},
        headers=_buy_headers(other_bearer, other_uid, published_event.id),
    )

    too_many = await client.post(
        "/v1/orders/",
        json={"event_id": published_event.id, "quantity": 2},   # 只剩 1 席
        headers={**common, "Idempotency-Key": str(uuid4())},
    )
    assert too_many.status_code == 409, too_many.text
    assert too_many.json()["available"] == 1, "要走庫存不足那條,不是限購"

    retry = await client.post(
        "/v1/orders/",
        json={"event_id": published_event.id, "quantity": 1},
        headers={**common, "Idempotency-Key": str(uuid4())},
    )
    assert retry.status_code == 202, retry.text
    assert await get_available(redis, event_id=published_event.id) == 0


@pytest.mark.asyncio
async def test_client_error_refunds_the_admission_token(client, db, published_event):
    """無座位圖的場次帶了 zone_id → 422,入場券必須退還讓使用者改正重送。"""
    bearer, uid = await _authed(client, db)
    token = create_admission_token(user_id=uid, event_id=published_event.id, ttl_seconds=120)
    common = {**bearer, "Admission-Token": token}

    bad_zone = await client.post(
        "/v1/orders/",
        json={"event_id": published_event.id, "quantity": 1, "zone_id": 999},
        headers={**common, "Idempotency-Key": str(uuid4())},
    )
    assert bad_zone.status_code == 422, bad_zone.text

    fixed = await client.post(
        "/v1/orders/",
        json={"event_id": published_event.id, "quantity": 1},
        headers={**common, "Idempotency-Key": str(uuid4())},
    )
    assert fixed.status_code == 202, fixed.text


@pytest.mark.asyncio
async def test_refund_admission_is_idempotent(redis):
    from app.services.waiting_room import refund_admission

    await refund_admission(redis, "never-issued")     # DEL 不存在的 key 是 no-op
    await refund_admission(redis, "never-issued")
