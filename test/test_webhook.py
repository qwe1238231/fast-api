import pytest
import stripe
from uuid import uuid4


async def _create_pending_order(client, drain, event_id):
    """註冊 + 登入 + 下單(202)+ 跑 worker 漆帳,回 (order_id, token)。"""
    await client.post("/v1/users/", json={"username": "alice", "password": "secret123"})
    r = await client.post("/v1/auth/token", data={"username": "alice", "password": "secret123"})
    token = r.json()["access_token"]
    await client.post(
        "/v1/orders/",
        json={"event_id": event_id, "quantity": 1},
        headers={"Authorization": f"Bearer {token}", "Idempotency-Key": str(uuid4())},
    )
    await drain()                                   # worker writes the order row
    auth = {"Authorization": f"Bearer {token}"}
    order_id = (await client.get("/v1/orders/me", headers=auth)).json()[0]["id"]
    return order_id, token


@pytest.mark.asyncio
async def test_webhook_confirms_order_on_payment_success(client, published_event, monkeypatch, drain_orders):
    order_id, token = await _create_pending_order(client, drain_orders, published_event.id)

    # 換掉簽章驗證,直接回傳一個 payment_intent.succeeded 事件
    def fake_construct_event(payload, sig_header, secret):
        return {
            "type": "payment_intent.succeeded",
            "data": {"object": {"metadata": {"order_id": str(order_id)}}},
        }
    monkeypatch.setattr("stripe.Webhook.construct_event", fake_construct_event)

    r = await client.post(
        "/v1/webhooks/stripe",
        content=b"{}",                              # 原始 body(端點用 request.body() 讀)
        headers={"Stripe-Signature": "whatever"},   # mock 會忽略它
    )
    assert r.status_code == 204

    # 訂單應該被推進到 confirmed
    rget = await client.get(
        f"/v1/orders/{order_id}", headers={"Authorization": f"Bearer {token}"}
    )
    assert rget.json()["status"] == "confirmed"


@pytest.mark.asyncio
async def test_webhook_rejects_bad_signature(client, monkeypatch):
    def raise_bad_sig(payload, sig_header, secret):
        raise stripe.SignatureVerificationError("bad signature", "sig")
    monkeypatch.setattr("stripe.Webhook.construct_event", raise_bad_sig)

    r = await client.post(
        "/v1/webhooks/stripe",
        content=b"{}",
        headers={"Stripe-Signature": "bad"},
    )
    assert r.status_code == 400


@pytest.mark.asyncio
async def test_webhook_ignores_event_without_order_id(client, monkeypatch):
    # 事件合法,但 metadata 沒有 order_id → 應該安靜略過,不報錯
    def fake(payload, sig_header, secret):
        return {"type": "payment_intent.succeeded", "data": {"object": {"metadata": {}}}}
    monkeypatch.setattr("stripe.Webhook.construct_event", fake)

    r = await client.post("/v1/webhooks/stripe", content=b"{}", headers={"Stripe-Signature": "x"})
    assert r.status_code == 204


@pytest.mark.asyncio
async def test_webhook_ignores_unknown_order(client, monkeypatch):
    # order_id 指向不存在的訂單 → 安靜略過
    def fake(payload, sig_header, secret):
        return {
            "type": "payment_intent.succeeded",
            "data": {"object": {"metadata": {"order_id": "999999"}}},
        }
    monkeypatch.setattr("stripe.Webhook.construct_event", fake)

    r = await client.post("/v1/webhooks/stripe", content=b"{}", headers={"Stripe-Signature": "x"})
    assert r.status_code == 204
