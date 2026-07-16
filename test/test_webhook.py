"""Stripe webhook tests (Part B: payment lifecycle).

Two layers:
- route-level (HTTP endpoint, signature mocked) for the plumbing;
- handler-level (calling the handlers directly with a mock Stripe client) for the
  refund / abort logic, where asserting whether a refund fired is cleanest.
"""
from datetime import datetime, timezone
from unittest.mock import AsyncMock, MagicMock
from uuid import uuid4

import pytest
import stripe
from sqlalchemy import select

from app.core.security import create_admission_token, get_password_hash
from app.models.user import User
from app.models.order import Order, OrderStatus
from app.services.inventory import reserve, get_available
from app.api.v1.webhook import _handle_payment_succeeded, _handle_payment_aborted


# ---------- route-level (HTTP endpoint + mocked signature) ----------

async def _create_pending_order(client, db, drain, event_id):
    """Register + login + order (202) + drain the worker -> (order_id, token)."""
    await client.post("/v1/users/", json={"username": "alice", "password": "secret123"})
    r = await client.post("/v1/auth/token", data={"username": "alice", "password": "secret123"})
    token = r.json()["access_token"]
    user_id = await db.scalar(select(User.id).where(User.username == "alice"))
    admission = create_admission_token(user_id=user_id, event_id=event_id, ttl_seconds=120)
    await client.post(
        "/v1/orders/",
        json={"event_id": event_id, "quantity": 1},
        headers={
            "Authorization": f"Bearer {token}",
            "Idempotency-Key": str(uuid4()),
            "Admission-Token": admission,
        },
    )
    await drain()
    auth = {"Authorization": f"Bearer {token}"}
    order_id = (await client.get("/v1/orders/me", headers=auth)).json()["items"][0]["id"]
    return order_id, token


@pytest.mark.asyncio
async def test_webhook_confirms_order_on_payment_success(client, db, published_event, monkeypatch, drain_orders):
    order_id, token = await _create_pending_order(client, db, drain_orders, published_event.id)

    def fake_construct_event(payload, sig_header, secret):
        return {
            "type": "payment_intent.succeeded",
            "data": {"object": {
                "id": "pi_test",
                "metadata": {"order_id": str(order_id)},
                "amount_received": 1500,           # matches the order total (price 1500 x1)
            }},
        }
    monkeypatch.setattr("stripe.Webhook.construct_event", fake_construct_event)

    r = await client.post("/v1/webhooks/stripe", content=b"{}", headers={"Stripe-Signature": "x"})
    assert r.status_code == 204
    rget = await client.get(f"/v1/orders/{order_id}", headers={"Authorization": f"Bearer {token}"})
    assert rget.json()["status"] == "confirmed"


@pytest.mark.asyncio
async def test_webhook_rejects_bad_signature(client, monkeypatch):
    def raise_bad_sig(payload, sig_header, secret):
        raise stripe.SignatureVerificationError("bad signature", "sig")
    monkeypatch.setattr("stripe.Webhook.construct_event", raise_bad_sig)
    r = await client.post("/v1/webhooks/stripe", content=b"{}", headers={"Stripe-Signature": "bad"})
    assert r.status_code == 400


@pytest.mark.asyncio
async def test_webhook_ignores_event_without_order_id(client, monkeypatch):
    def fake(payload, sig_header, secret):
        return {"type": "payment_intent.succeeded", "data": {"object": {"metadata": {}}}}
    monkeypatch.setattr("stripe.Webhook.construct_event", fake)
    r = await client.post("/v1/webhooks/stripe", content=b"{}", headers={"Stripe-Signature": "x"})
    assert r.status_code == 204


@pytest.mark.asyncio
async def test_webhook_ignores_unknown_order(client, monkeypatch):
    def fake(payload, sig_header, secret):
        return {"type": "payment_intent.succeeded",
                "data": {"object": {"metadata": {"order_id": "999999"}, "amount_received": 1}}}
    monkeypatch.setattr("stripe.Webhook.construct_event", fake)
    r = await client.post("/v1/webhooks/stripe", content=b"{}", headers={"Stripe-Signature": "x"})
    assert r.status_code == 204


# ---------- handler-level (direct calls, mock Stripe for refund assertions) ----------

def _stripe_mock() -> MagicMock:
    m = MagicMock()
    m.v1.refunds.create_async = AsyncMock(return_value=MagicMock(id="re_1", status="succeeded"))
    return m


def _intent(order: Order, *, amount: int | None = None) -> dict:
    return {
        "id": "pi_test_1",
        "metadata": {"order_id": str(order.id)},
        "amount_received": order.total_price_cents if amount is None else amount,
    }


async def _order(db, event, *, status: OrderStatus = OrderStatus.PENDING, cents: int = 1500) -> Order:
    user = User(username=f"u-{uuid4().hex[:8]}", hashed_password=get_password_hash("x"))
    db.add(user)
    await db.flush()
    now = datetime.now(timezone.utc)
    # terminal statuses require their matching timestamp (orders CHECK constraints)
    ts = {
        OrderStatus.PAID: {"paid_at": now},
        OrderStatus.CONFIRMED: {"confirmed_at": now},
        OrderStatus.EXPIRED: {"expired_at": now},
        OrderStatus.CANCELLED: {"cancelled_at": now},
    }.get(status, {})
    order = Order(
        user_id=user.id, event_id=event.id, quantity=1, total_price_cents=cents,
        idempotency_key=uuid4(), status=status, payment_provider_id="pi_test_1", **ts,
    )
    db.add(order)
    await db.commit()
    return order


@pytest.mark.asyncio
async def test_handler_refunds_on_amount_mismatch(db, published_event):
    order = await _order(db, published_event, cents=1500)
    sm = _stripe_mock()
    await _handle_payment_succeeded(db, sm, _intent(order, amount=999))   # underpaid
    await db.refresh(order)
    assert order.status == OrderStatus.PENDING                            # NOT confirmed
    assert sm.v1.refunds.create_async.call_count == 1                     # refunded


@pytest.mark.asyncio
async def test_handler_refunds_when_order_already_terminal(db, published_event):
    order = await _order(db, published_event, status=OrderStatus.EXPIRED)
    sm = _stripe_mock()
    await _handle_payment_succeeded(db, sm, _intent(order))
    await db.refresh(order)
    assert order.status == OrderStatus.EXPIRED                            # unchanged
    assert sm.v1.refunds.create_async.call_count == 1                     # charged too late -> refund


@pytest.mark.asyncio
async def test_handler_duplicate_delivery_is_noop(db, published_event):
    order = await _order(db, published_event, status=OrderStatus.CONFIRMED)
    sm = _stripe_mock()
    await _handle_payment_succeeded(db, sm, _intent(order))
    await db.refresh(order)
    assert order.status == OrderStatus.CONFIRMED
    assert sm.v1.refunds.create_async.call_count == 0                     # already paid -> no-op


@pytest.mark.asyncio
async def test_handler_aborted_releases_seat(db, redis, published_event):
    order = await _order(db, published_event)
    await reserve(redis, event_id=published_event.id, quantity=1)         # 5 -> 4
    await _handle_payment_aborted(db, redis, _intent(order))
    await db.refresh(order)
    assert order.status == OrderStatus.EXPIRED
    assert await get_available(redis, event_id=published_event.id) == 5   # seat released
