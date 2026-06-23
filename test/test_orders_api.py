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
async def test_create_order_succeeds(client, published_event, redis):
    headers = await auth_headers(client)
    headers["Idempotency-Key"] = str(uuid4())

    r = await client.post(
        "/v1/orders/",
        json={"event_id": published_event.id, "quantity": 2},
        headers=headers,
    )

    assert r.status_code == 201
    assert r.json()["status"] == "pending"
    assert r.json()["quantity"] == 2
    assert await get_available(redis, event_id=published_event.id) == 3   # 5 - 2


@pytest.mark.asyncio
async def test_sold_out_returns_409(client, published_event):
    headers = await auth_headers(client)

    r1 = await client.post(                       # 先把 5 張全買走
        "/v1/orders/",
        json={"event_id": published_event.id, "quantity": 5},
        headers={**headers, "Idempotency-Key": str(uuid4())},
    )
    assert r1.status_code == 201

    r2 = await client.post(                       # 再買 → 售完
        "/v1/orders/",
        json={"event_id": published_event.id, "quantity": 1},
        headers={**headers, "Idempotency-Key": str(uuid4())},
    )
    assert r2.status_code == 409


@pytest.mark.asyncio
async def test_idempotent_create_returns_same_order(client, published_event, redis):
    headers = await auth_headers(client)
    key = str(uuid4())
    body = {"event_id": published_event.id, "quantity": 1}

    r1 = await client.post("/v1/orders/", json=body, headers={**headers, "Idempotency-Key": key})
    r2 = await client.post("/v1/orders/", json=body, headers={**headers, "Idempotency-Key": key})

    assert r1.status_code == 201 and r2.status_code == 201
    assert r1.json()["id"] == r2.json()["id"]                              # 同一筆,不重複建
    assert await get_available(redis, event_id=published_event.id) == 4    # 只扣一次(5-1)