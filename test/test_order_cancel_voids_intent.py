"""`POST /orders/{id}/cancel` 也要讓 Stripe 那邊的 PaymentIntent 死掉。

以前取消只動本地:intent 留在 requires_payment_method,拿著 client_secret 仍然付得進去,
然後才靠 webhook 退款。現在 post-commit 補一刀 cancel。這裡每條都在驗同一件事:
**取消本身永遠成功(204、CANCELLED、座位回來)**,Stripe 那一刀是 best-effort。
"""
import logging
from uuid import uuid4

import pytest
import stripe
from sqlalchemy import select, update

from app.core.security import create_admission_token
from app.models.order import Order
from app.models.user import User
from app.services.inventory import get_available

INTENT = "pi_live_1"


async def _pending_order(client, db, drain, event_id: int, *, intent: str | None) -> tuple[int, dict]:
    """註冊 → 下單(202)→ 落帳 → 視需要掛上 payment_provider_id。回 (order_id, auth)。"""
    name = f"canceller_{uuid4().hex[:8]}"   # 註冊規則是 ^[a-zA-Z0-9_]+$,連字號會被擋
    await client.post("/v1/users/", json={"username": name, "password": "secret123"})
    token = (
        await client.post("/v1/auth/token", data={"username": name, "password": "secret123"})
    ).json()["access_token"]
    user_id = await db.scalar(select(User.id).where(User.username == name))
    await client.post(
        "/v1/orders/",
        json={"event_id": event_id, "quantity": 1},
        headers={
            "Authorization": f"Bearer {token}",
            "Idempotency-Key": str(uuid4()),
            "Admission-Token": create_admission_token(user_id=user_id, event_id=event_id, ttl_seconds=120),
        },
    )
    await drain()
    auth = {"Authorization": f"Bearer {token}"}
    order_id = (await client.get("/v1/orders/me", headers=auth)).json()["items"][0]["id"]
    if intent is not None:
        # 直接寫欄位而不是打 /payment-intent:那條會真的呼叫 Stripe(mock 上沒裝
        # create_async),而且這裡要測的是取消,不是建 intent。
        await db.execute(update(Order).where(Order.id == order_id).values(payment_provider_id=intent))
        await db.commit()
    return order_id, auth


def _stripe(client):
    return client._transport.app.state.stripe


def _events(caplog) -> set[str]:
    return {getattr(rec, "event", None) for rec in caplog.records}


@pytest.mark.asyncio
async def test_cancel_voids_the_live_intent_as_requested_by_customer(client, db, redis, published_event, drain_orders):
    order_id, auth = await _pending_order(client, db, drain_orders, published_event.id, intent=INTENT)
    before = await get_available(redis, event_id=published_event.id)

    r = await client.post(f"/v1/orders/{order_id}/cancel", headers=auth)

    assert r.status_code == 204
    assert (await client.get(f"/v1/orders/{order_id}", headers=auth)).json()["status"] == "cancelled"
    assert await get_available(redis, event_id=published_event.id) == before + 1
    _stripe(client).v1.payment_intents.cancel_async.assert_awaited_once_with(
        INTENT, {"cancellation_reason": "requested_by_customer"}
    )


@pytest.mark.asyncio
async def test_cancel_without_an_intent_never_talks_to_stripe(client, db, published_event, drain_orders):
    """還沒按付款的訂單沒有 intent 可取消。多打一次 Stripe 是浪費,而且會對一個 None 的 id 報錯。"""
    order_id, auth = await _pending_order(client, db, drain_orders, published_event.id, intent=None)

    r = await client.post(f"/v1/orders/{order_id}/cancel", headers=auth)

    assert r.status_code == 204
    _stripe(client).v1.payment_intents.cancel_async.assert_not_awaited()


@pytest.mark.asyncio
async def test_cancel_still_succeeds_when_stripe_is_down(client, db, redis, published_event, drain_orders, caplog):
    """Stripe 掛了不能讓使用者取消不了。取消在 commit 那一刻已生效,Stripe 那刀失敗只記 WARNING
    —— 不是 alert,因為後果(之後若付了就退款)有全自動的路。"""
    order_id, auth = await _pending_order(client, db, drain_orders, published_event.id, intent=INTENT)
    before = await get_available(redis, event_id=published_event.id)
    _stripe(client).v1.payment_intents.cancel_async.side_effect = stripe.APIConnectionError("boom")

    with caplog.at_level(logging.WARNING, logger="app.api.v1.orders"):
        r = await client.post(f"/v1/orders/{order_id}/cancel", headers=auth)

    assert r.status_code == 204
    assert (await client.get(f"/v1/orders/{order_id}", headers=auth)).json()["status"] == "cancelled"
    assert await get_available(redis, event_id=published_event.id) == before + 1
    assert "cancel_void_intent_failed" in _events(caplog)
    assert not any(getattr(rec, "needs_human", False) for rec in caplog.records)


@pytest.mark.asyncio
async def test_cancel_when_the_charge_already_landed_leaves_it_to_the_webhook(client, db, published_event, drain_orders, caplog):
    """使用者按取消的同一瞬間付款成功:本地照樣 CANCELLED,Stripe 說 succeeded 就只記下來。
    退款是 `succeeded` webhook 的事(它會看到 CANCELLED 而走 _RefundNeeded),這裡不做第二件事。"""
    order_id, auth = await _pending_order(client, db, drain_orders, published_event.id, intent=INTENT)
    _stripe(client).v1.payment_intents.cancel_async.side_effect = stripe.InvalidRequestError(
        "cannot cancel", "intent", code="payment_intent_unexpected_state",
        json_body={"error": {
            "code": "payment_intent_unexpected_state",
            "payment_intent": {"id": INTENT, "status": "succeeded"},
        }},
    )

    with caplog.at_level(logging.INFO, logger="app.api.v1.orders"):
        r = await client.post(f"/v1/orders/{order_id}/cancel", headers=auth)

    assert r.status_code == 204
    assert (await client.get(f"/v1/orders/{order_id}", headers=auth)).json()["status"] == "cancelled"
    [rec] = [r for r in caplog.records if getattr(r, "event", None) == "cancel_intent_not_voidable"]
    assert rec.intent_status == "succeeded"
    _stripe(client).v1.refunds.create_async.assert_not_awaited()   # 退款不是這條路的事
