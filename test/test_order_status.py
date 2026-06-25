"""Tests for GET /orders/by-key/{idempotency_key} — the method-A poll endpoint."""
from uuid import uuid4

import pytest


async def _auth(client, username):
    await client.post("/v1/users/", json={"username": username, "password": "secret123"})
    r = await client.post("/v1/auth/token", data={"username": username, "password": "secret123"})
    return {"Authorization": f"Bearer {r.json()['access_token']}"}


async def _create(client, headers, event_id, key, quantity=1):
    return await client.post(
        "/v1/orders/",
        json={"event_id": event_id, "quantity": quantity},
        headers={**headers, "Idempotency-Key": key},
    )


@pytest.mark.asyncio
async def test_status_processing_before_worker(client, published_event):
    """Created (202) but worker hasn't run -> processing, no order yet."""
    headers = await _auth(client, "alice")
    key = str(uuid4())
    await _create(client, headers, published_event.id, key)

    r = await client.get(f"/v1/orders/by-key/{key}", headers=headers)
    assert r.status_code == 200
    assert r.json()["state"] == "processing"
    assert r.json()["order"] is None


@pytest.mark.asyncio
async def test_status_ready_after_worker(client, published_event, drain_orders):
    """After the worker persists it -> ready, the order is returned."""
    headers = await _auth(client, "alice")
    key = str(uuid4())
    await _create(client, headers, published_event.id, key)
    await drain_orders()

    r = await client.get(f"/v1/orders/by-key/{key}", headers=headers)
    assert r.status_code == 200
    assert r.json()["state"] == "ready"
    assert r.json()["order"]["status"] == "pending"
    assert r.json()["order"]["quantity"] == 1


@pytest.mark.asyncio
async def test_status_failed_after_giveup(client, published_event, redis):
    """When the claim is marked FAILED (worker gave up) -> failed."""
    from app.services.idempotency import mark_claim_failed

    headers = await _auth(client, "alice")
    key = str(uuid4())
    await _create(client, headers, published_event.id, key)
    await mark_claim_failed(redis, idempotency_key=key)

    r = await client.get(f"/v1/orders/by-key/{key}", headers=headers)
    assert r.status_code == 200
    assert r.json()["state"] == "failed"
    assert r.json()["order"] is None


@pytest.mark.asyncio
async def test_status_unknown_key_404(client):
    headers = await _auth(client, "alice")
    r = await client.get(f"/v1/orders/by-key/{uuid4()}", headers=headers)
    assert r.status_code == 404


@pytest.mark.asyncio
async def test_status_other_users_key_404(client, published_event, drain_orders):
    """A key that belongs to someone else -> 404 (don't leak its existence)."""
    alice = await _auth(client, "alice")
    key = str(uuid4())
    await _create(client, alice, published_event.id, key)
    await drain_orders()

    bob = await _auth(client, "bob")
    r = await client.get(f"/v1/orders/by-key/{key}", headers=bob)
    assert r.status_code == 404
