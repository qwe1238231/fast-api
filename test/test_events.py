from uuid import uuid4

import pytest

from app.core.security import get_password_hash
from app.models.user import User
from app.services.inventory import get_available


async def _make_admin_and_login(client, db, username="admin"):
    db.add(User(username=username, hashed_password=get_password_hash("secret123"), is_admin=True))
    await db.commit()
    r = await client.post("/v1/auth/token", data={"username": username, "password": "secret123"})
    return {"Authorization": f"Bearer {r.json()['access_token']}"}


def _event_payload():
    return {
        "name": "Concert",
        "venue": "Arena",
        "starts_at": "2026-12-01T19:00:00+00:00",
        "ends_at": "2026-12-01T22:00:00+00:00",
        "sale_starts_at": "2026-06-01T00:00:00+00:00",
        "sale_ends_at": "2026-11-30T23:59:00+00:00",
        "total_seats": 100,
        "price_cents": 1500,
    }


@pytest.mark.asyncio
async def test_create_and_publish_event_seeds_inventory(client, db, redis):
    headers = await _make_admin_and_login(client, db)

    r = await client.post("/v1/events/", json=_event_payload(), headers=headers)
    assert r.status_code == 201
    assert r.json()["status"] == "draft"
    event_id = r.json()["id"]

    rp = await client.post(f"/v1/events/{event_id}/publish", headers=headers)
    assert rp.status_code == 200
    assert rp.json()["status"] == "published"
    assert await get_available(redis, event_id=event_id) == 100   # 死碼 set_initial_stock 接上了


@pytest.mark.asyncio
async def test_non_admin_cannot_create_event(client):
    await client.post("/v1/users/", json={"username": "bob", "password": "secret123"})
    r = await client.post("/v1/auth/token", data={"username": "bob", "password": "secret123"})
    headers = {"Authorization": f"Bearer {r.json()['access_token']}"}

    r2 = await client.post("/v1/events/", json=_event_payload(), headers=headers)
    assert r2.status_code == 403


@pytest.mark.asyncio
async def test_publish_non_draft_is_rejected(client, db):
    headers = await _make_admin_and_login(client, db)
    event_id = (await client.post("/v1/events/", json=_event_payload(), headers=headers)).json()["id"]
    await client.post(f"/v1/events/{event_id}/publish", headers=headers)        # draft → published
    # 再發佈一次 → 已不是 draft → 400
    again = await client.post(f"/v1/events/{event_id}/publish", headers=headers)
    assert again.status_code == 400


@pytest.mark.asyncio
async def test_reconcile_rebuilds_inventory_after_redis_loss(client, db, redis):
    headers = await _make_admin_and_login(client, db)
    event_id = (await client.post("/v1/events/", json=_event_payload(), headers=headers)).json()["id"]
    await client.post(f"/v1/events/{event_id}/publish", headers=headers)        # 庫存 100

    # 下 3 筆共 30 張 → Redis 剩 70、DB 有 30 的 pending 訂單
    for _ in range(3):
        r = await client.post(
            "/v1/orders/",
            json={"event_id": event_id, "quantity": 10},
            headers={**headers, "Idempotency-Key": str(uuid4())},
        )
        assert r.status_code == 201
    assert await get_available(redis, event_id=event_id) == 70

    # 模擬 Redis 遺失那個 key
    await redis.delete(f"event:{event_id}:available")
    assert await get_available(redis, event_id=event_id) == 0

    # reconcile 從 Postgres 重建:100 - 30 = 70
    rr = await client.post(f"/v1/events/{event_id}/reconcile-inventory", headers=headers)
    assert rr.status_code == 200
    assert rr.json()["available"] == 70
    assert await get_available(redis, event_id=event_id) == 70
