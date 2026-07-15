from datetime import datetime, timedelta, timezone
from uuid import uuid4

import pytest

from sqlalchemy import select

from app.core.security import get_password_hash, create_admission_token
from app.models.user import User
from app.services.inventory import get_available
from app.services.inventory import reconcile_inventory
from app.core.exceptions import InventoryNotReconcilable
from app.services import waiting_room as wr

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


async def _order_headers(db, headers, event_id, username="admin"):
    """auth + a fresh admission token (bypasses the queue) + fresh idem key."""
    uid = await db.scalar(select(User.id).where(User.username == username))
    return {
        **headers,
        "Admission-Token": create_admission_token(user_id=uid, event_id=event_id, ttl_seconds=120),
        "Idempotency-Key": str(uuid4()),
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
async def test_reconcile_rebuilds_inventory_after_redis_loss(client, db, redis, drain_orders):
    headers = await _make_admin_and_login(client, db)
    event_id = (await client.post("/v1/events/", json=_event_payload(), headers=headers)).json()["id"]
    await client.post(f"/v1/events/{event_id}/publish", headers=headers)        # 庫存 100

    # 下 3 筆共 30 張 → Redis 立刻剩 70,worker 漆帳後 DB 有 30 的 pending 訂單
    for _ in range(3):
        r = await client.post(
            "/v1/orders/",
            json={"event_id": event_id, "quantity": 10},
            headers=await _order_headers(db, headers, event_id),
        )
        assert r.status_code == 202
    assert await get_available(redis, event_id=event_id) == 70
    await drain_orders()                                                        # 30 筆寫進 Postgres

    # 模擬 Redis 遺失那個 key
    await redis.delete(f"event:{event_id}:available")
    assert await get_available(redis, event_id=event_id) == 0

    # reconcile 從 Postgres 重建:100 - 30 = 70
    available = await reconcile_inventory(db, redis, event_id=event_id)
    assert available == 70
    assert await get_available(redis, event_id=event_id) == 70

@pytest.mark.asyncio
async def test_reconcile_refuses_when_stream_not_drained(client, db, redis):
    headers = await _make_admin_and_login(client, db)
    event_id = (await client.post("/v1/events/", json=_event_payload(), headers=headers)).json()["id"]
    await client.post(f"/v1/events/{event_id}/publish", headers=headers)

    # 下一單 → 進 stream,但故意「不 drain」→ backlog > 0
    await client.post(
        "/v1/orders/",
        json={"event_id": event_id, "quantity": 1},
        headers=await _order_headers(db, headers, event_id),
    )

    # 沒排空 + force=False(預設)→ 守衛應 raise
    with pytest.raises(InventoryNotReconcilable):
        await reconcile_inventory(db, redis, event_id=event_id)

@pytest.mark.asyncio
async def test_reconcile_force_bypasses_guard(client, db, redis):
    headers = await _make_admin_and_login(client, db)
    event_id = (await client.post("/v1/events/", json=_event_payload(), headers=headers)).json()["id"]
    await client.post(f"/v1/events/{event_id}/publish", headers=headers)

    # 下一單 → stream 有 backlog,一樣不 drain
    await client.post(
        "/v1/orders/",
        json={"event_id": event_id, "quantity": 1},
        headers=await _order_headers(db, headers, event_id),
    )

    # force=True → 即使沒排空也不 raise,正常回傳
    available = await reconcile_inventory(db, redis, event_id=event_id, force=True)
    assert isinstance(available, int)

@pytest.mark.asyncio
async def test_detect_inventory_drift(client, db, redis):
    from app.worker import detect_inventory_drift

    headers = await _make_admin_and_login(client, db)
    event_id = (await client.post("/v1/events/", json=_event_payload(), headers=headers)).json()["id"]
    await client.post(f"/v1/events/{event_id}/publish", headers=headers)        # 庫存 100,無訂單

    # 一致 → 沒有 drift
    assert await detect_inventory_drift({"redis_client": redis}) == []

    # 故意把 Redis 設成錯的值 → 應被抓到
    await redis.set(f"event:{event_id}:available", 42)
    drifts = await detect_inventory_drift({"redis_client": redis})
    assert drifts == [{"event_id": event_id, "expected": 100, "actual": 42}]

@pytest.mark.asyncio
async def test_drift_check_skipped_when_queue_not_drained(client, db, redis):
    from app.worker import detect_inventory_drift

    headers = await _make_admin_and_login(client, db)
    event_id = (await client.post("/v1/events/", json=_event_payload(), headers=headers)).json()["id"]
    await client.post(f"/v1/events/{event_id}/publish", headers=headers)

    # 下一單 → stream 有 backlog,故意不 drain
    await client.post(
        "/v1/orders/",
        json={"event_id": event_id, "quantity": 1},
        headers=await _order_headers(db, headers, event_id),
    )

    # 把 Redis 設成明顯錯的值 → 沒閘門的話這一定會被報成 drift
    await redis.set(f"event:{event_id}:available", 42)

    # 但因為 backlog > 0,drift 檢查應直接跳過、回 []
    assert await detect_inventory_drift({"redis_client": redis}) == []


@pytest.mark.asyncio
async def test_get_event_meta_caches(db, redis, published_event):
    from app.services.event_cache import get_event_meta

    key = f"event:{published_event.id}:meta"
    assert await redis.get(key) is None                       # 一開始沒快取
    meta = await get_event_meta(redis, db, event_id=published_event.id)
    assert meta.status.value == "published"
    assert await redis.get(key) is not None                   # 讀過 → 快取了(下次不打 DB)


@pytest.mark.asyncio
async def test_publish_invalidates_event_cache(client, db, redis):
    from app.services.event_cache import get_event_meta

    headers = await _make_admin_and_login(client, db)
    event_id = (await client.post("/v1/events/", json=_event_payload(), headers=headers)).json()["id"]

    await get_event_meta(redis, db, event_id=event_id)        # 草稿狀態被快取
    assert await redis.get(f"event:{event_id}:meta") is not None

    await client.post(f"/v1/events/{event_id}/publish", headers=headers)
    assert await redis.get(f"event:{event_id}:meta") is None  # 發佈清掉了快取


def _payload_sale_in(seconds: int) -> dict:
    now = datetime.now(timezone.utc)
    return {
        "name": "Concert", "venue": "Arena",
        "starts_at": (now + timedelta(days=30)).isoformat(),
        "ends_at": (now + timedelta(days=30, hours=3)).isoformat(),
        "sale_starts_at": (now + timedelta(seconds=seconds)).isoformat(),
        "sale_ends_at": (now + timedelta(days=1)).isoformat(),
        "total_seats": 100, "price_cents": 1000,
    }


async def _publish_event(client, db, payload) -> tuple[int, dict]:
    headers = await _make_admin_and_login(client, db)
    event_id = (await client.post("/v1/events/", json=payload, headers=headers)).json()["id"]
    await client.post(f"/v1/events/{event_id}/publish", headers=headers)
    return event_id, headers


@pytest.mark.asyncio
async def test_queue_register_and_position(client, db):
    # sale in 5 min → fallback window is OPEN now (opens sale-10m past, closes sale-30s future)
    event_id, headers = await _publish_event(client, db, _payload_sale_in(300))

    r = await client.post(f"/v1/events/{event_id}/queue", headers=headers)
    assert r.status_code == 200
    assert r.json()["admitted"] is False
    assert r.json()["people_ahead"] == 0        # first registrant, admission not started

    s = await client.get(f"/v1/events/{event_id}/queue/status", headers=headers)
    assert s.json()["admitted"] is False


@pytest.mark.asyncio
async def test_queue_admits_after_window_closes(client, db, redis):
    event_id, headers = await _publish_event(client, db, _payload_sale_in(300))
    await client.post(f"/v1/events/{event_id}/queue", headers=headers)   # rank 0

    # simulate the window having closed 10s ago → RATE*10 admitted, rank 0 is in
    past = (datetime.now(timezone.utc) - timedelta(seconds=10)).timestamp()
    await redis.set(wr._admit_start_key(event_id), past)

    s = await client.get(f"/v1/events/{event_id}/queue/status", headers=headers)
    assert s.json()["admitted"] is True


@pytest.mark.asyncio
async def test_queue_registration_closed(client, db):
    # sale already started → fallback close (sale-30s) is in the past → registration closed
    event_id, headers = await _publish_event(client, db, _payload_sale_in(0))
    r = await client.post(f"/v1/events/{event_id}/queue", headers=headers)
    assert r.status_code == 409


@pytest.mark.asyncio
async def test_queue_sold_out_stops_admission(client, db, redis):
    event_id, headers = await _publish_event(client, db, _payload_sale_in(300))
    await client.post(f"/v1/events/{event_id}/queue", headers=headers)   # register (rank 0)

    # window closed long ago (would normally admit) BUT inventory exhausted
    past = (datetime.now(timezone.utc) - timedelta(seconds=60)).timestamp()
    await redis.set(wr._admit_start_key(event_id), past)
    await redis.set(f"event:{event_id}:available", 0)

    s = (await client.get(f"/v1/events/{event_id}/queue/status", headers=headers)).json()
    assert s["sold_out"] is True
    assert s["admitted"] is False          # sold-out short-circuits admission
    assert s["access_token"] is None       # no pass handed out into a dead sale


@pytest.mark.asyncio
async def test_queue_admission_paused_by_circuit_breaker(client, db, redis):
    event_id, headers = await _publish_event(client, db, _payload_sale_in(300))
    await client.post(f"/v1/events/{event_id}/queue", headers=headers)   # register (rank 0)

    past = (datetime.now(timezone.utc) - timedelta(seconds=60)).timestamp()
    await redis.set(wr._admit_start_key(event_id), past)                 # would admit...
    await wr.set_admission_paused(redis, True)                           # ...but breaker is open

    s = (await client.get(f"/v1/events/{event_id}/queue/status", headers=headers)).json()
    assert s["paused"] is True
    assert s["admitted"] is False         # admission frozen; position held
    assert s["access_token"] is None


@pytest.mark.asyncio
async def test_queue_join_rate_limited(client, db, monkeypatch):
    from app.core.config import get_settings
    monkeypatch.setattr(get_settings(), "QUEUE_JOIN_LIMIT_PER_MINUTE", 2)
    event_id, headers = await _publish_event(client, db, _payload_sale_in(300))

    codes = [
        (await client.post(f"/v1/events/{event_id}/queue", headers=headers)).status_code
        for _ in range(3)
    ]
    assert codes == [200, 200, 429]     # 3rd join in the window is throttled