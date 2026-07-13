import pytest
from uuid import uuid4

from app.services.inventory import get_available


async def auth_headers(client, username="alice"):
    await client.post("/v1/users/", json={"username": username, "password": "secret123"})
    r = await client.post(
        "/v1/auth/token", data={"username": username, "password": "secret123"}
    )
    return {"Authorization": f"Bearer {r.json()['access_token']}"}


@pytest.mark.asyncio
async def test_create_order_succeeds(client, published_event, redis, drain_orders):
    headers = await auth_headers(client)
    headers["Idempotency-Key"] = str(uuid4())

    r = await client.post(
        "/v1/orders/",
        json={"event_id": published_event.id, "quantity": 2},
        headers=headers,
    )

    assert r.status_code == 202                                            # accepted, processing
    assert r.json()["status"] == "processing"
    assert await get_available(redis, event_id=published_event.id) == 3    # seat reserved at once (5 - 2)

    await drain_orders()                                                   # worker persists it
    orders = (await client.get("/v1/orders/me", headers=headers)).json()["items"]
    assert len(orders) == 1
    assert orders[0]["status"] == "pending"
    assert orders[0]["quantity"] == 2


@pytest.mark.asyncio
async def test_sold_out_returns_409(client, published_event):
    headers = await auth_headers(client)

    r1 = await client.post(                       # 先把 5 張全買走
        "/v1/orders/",
        json={"event_id": published_event.id, "quantity": 5},
        headers={**headers, "Idempotency-Key": str(uuid4())},
    )
    assert r1.status_code == 202

    r2 = await client.post(                       # 再買 → 售完
        "/v1/orders/",
        json={"event_id": published_event.id, "quantity": 1},
        headers={**headers, "Idempotency-Key": str(uuid4())},
    )
    assert r2.status_code == 409


@pytest.mark.asyncio
async def test_idempotent_create_returns_same_order(client, published_event, redis, drain_orders):
    headers = await auth_headers(client)
    key = str(uuid4())
    body = {"event_id": published_event.id, "quantity": 1}

    r1 = await client.post("/v1/orders/", json=body, headers={**headers, "Idempotency-Key": key})
    r2 = await client.post("/v1/orders/", json=body, headers={**headers, "Idempotency-Key": key})

    assert r1.status_code == 202 and r2.status_code == 202
    assert r1.json()["idempotency_key"] == r2.json()["idempotency_key"]    # 同一個 handle
    assert await get_available(redis, event_id=published_event.id) == 4    # 只扣一次(5-1)

    await drain_orders()
    orders = (await client.get("/v1/orders/me", headers=headers)).json()["items"]
    assert len(orders) == 1                                                # 只建了一筆,沒重複


@pytest.mark.asyncio
async def test_orders_me_keyset_pagination(client, published_event, drain_orders):
    headers = await auth_headers(client)
    for _ in range(3):                                                     # 建 3 筆訂單
        r = await client.post(
            "/v1/orders/",
            json={"event_id": published_event.id, "quantity": 1},
            headers={**headers, "Idempotency-Key": str(uuid4())},
        )
        assert r.status_code == 202
    await drain_orders()

    page1 = (await client.get("/v1/orders/me?limit=2", headers=headers)).json()
    assert len(page1["items"]) == 2
    assert page1["next_cursor"] is not None                                # 還有下一頁

    page2 = (
        await client.get(f"/v1/orders/me?limit=2&cursor={page1['next_cursor']}", headers=headers)
    ).json()
    assert len(page2["items"]) == 1
    assert page2["next_cursor"] is None                                    # 最後一頁

    ids = [o["id"] for o in page1["items"]] + [o["id"] for o in page2["items"]]
    assert len(set(ids)) == 3                                              # 不重疊、不遺漏
    assert ids == sorted(ids, reverse=True)                                # 跨頁維持新→舊

    bad = await client.get("/v1/orders/me?cursor=not-a-valid-cursor", headers=headers)
    assert bad.status_code == 422                                          # 壞游標被擋