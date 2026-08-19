"""Stripe webhook tests (Part B: payment lifecycle).

Two層:
- route-level(HTTP 端點,簽章 mock 掉)—— 管線、去重、以及退款到底有沒有真的發出去;
- handler-level(直接呼叫 handler)—— 「要不要退款」這個**決定**。handler 已經不碰
  網路了,所以這一層斷言的是回傳的決定,而不是有沒有呼叫 Stripe。
"""
import asyncio
from datetime import datetime, timedelta, timezone
from uuid import uuid4

import pytest
import stripe
from sqlalchemy import func, select

from app.core.security import create_admission_token, get_password_hash
from app.crud.stripe_event import claim_event, cutoff_for, purge_events_older_than
from app.db.session import AsyncSessionLocal
from app.models.stripe_event import StripeEvent
from app.models.user import User
from app.models.order import Order, OrderStatus
from app.services.inventory import get_available
from app.api.v1.webhook import (
    _RefundNeeded,
    _handle_payment_aborted,
    _handle_payment_succeeded,
)


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


def _succeeded_event(order_id: int, *, event_id: str, amount: int = 1500) -> dict:
    """一個 `payment_intent.succeeded` 事件。`event_id` 是**去重的鍵**。"""
    return {
        "id": event_id,
        "type": "payment_intent.succeeded",
        "data": {"object": {
            "id": "pi_test",
            "metadata": {"order_id": str(order_id)},
            "amount_received": amount,
        }},
    }


@pytest.mark.asyncio
async def test_webhook_confirms_order_on_payment_success(client, db, published_event, monkeypatch, drain_orders):
    order_id, token = await _create_pending_order(client, db, drain_orders, published_event.id)
    monkeypatch.setattr(
        "stripe.Webhook.construct_event",
        lambda payload, sig_header, secret: _succeeded_event(order_id, event_id="evt_1"),
    )

    r = await client.post("/v1/webhooks/stripe", content=b"{}", headers={"Stripe-Signature": "x"})
    assert r.status_code == 204
    rget = await client.get(f"/v1/orders/{order_id}", headers={"Authorization": f"Bearer {token}"})
    assert rget.json()["status"] == "confirmed"


@pytest.mark.asyncio
async def test_concurrent_claims_of_one_event_are_mutually_exclusive():
    """同一個事件被並發 claim 時,**恰好一個**拿到。

    整個「不會重複處理」的論證就架在這條性質上,而它不是我們自己寫的邏輯 —— 是
    `ON CONFLICT DO NOTHING` 配唯一索引:第二個 INSERT 會等第一個交易結束,然後發現
    已經有人了。所以這裡直接驗那條性質,不繞過路由。

    (先 SELECT 再 INSERT 的寫法在這條測試下會壞:兩邊都會看到「還沒有」而雙雙回 True。)
    """
    async def claim() -> bool:
        async with AsyncSessionLocal() as session:
            claimed = await claim_event(session, event_id="evt_race", event_type="x")
            await session.commit()
            return claimed

    results = await asyncio.gather(claim(), claim())

    assert sorted(results) == [False, True]


@pytest.mark.asyncio
async def test_replayed_event_does_not_reprocess(client, db, published_event, monkeypatch, drain_orders):
    """**這條擋的是一個會真的把錢退出去的重送。**

    Stripe 會重送同一個事件(逾時、它自己的 at-least-once 保證)。沒有去重的話,第二次
    投遞會照完整流程跑一遍 —— 而流程的判斷是看**訂單當下的狀態**,不是看「這個事件
    處理過了嗎」。所以只要訂單在兩次投遞之間離開了可付款狀態(過期 cron 跑到、使用者
    取消),第二次就會判定「收錢收得太晚」而**發動退款**:同一個人拿到票、又拿到退款。

    這裡刻意在兩次投遞之間把訂單改成 EXPIRED 來製造那個狀態。沒有退款發生,就證明
    去重是在讀訂單**之前**就攔下來了。

    (實測過:把 `claim_event` 改成永遠回 True,這條會紅在 `refunds.call_count == 0`。)
    """
    order_id, _ = await _create_pending_order(client, db, drain_orders, published_event.id)
    monkeypatch.setattr(
        "stripe.Webhook.construct_event",
        lambda payload, sig_header, secret: _succeeded_event(order_id, event_id="evt_replay"),
    )
    refunds = client._transport.app.state.stripe.v1.refunds.create_async

    await client.post("/v1/webhooks/stripe", content=b"{}", headers={"Stripe-Signature": "x"})
    order = await db.get(Order, order_id)
    order.status, order.expired_at = OrderStatus.EXPIRED, datetime.now(timezone.utc)
    await db.commit()

    r = await client.post("/v1/webhooks/stripe", content=b"{}", headers={"Stripe-Signature": "x"})

    assert r.status_code == 204
    assert refunds.call_count == 0


@pytest.mark.asyncio
async def test_distinct_events_are_both_processed(client, db, published_event, monkeypatch, drain_orders):
    """去重不能太寬:不同事件 id 就是不同事件,即使指向同一筆訂單。

    (第二個事件會因為訂單已經 CONFIRMED 而 no-op —— 重點是它**有被處理**,兩筆都進
    了去重表,而不是被當成重送丟掉。)
    """
    order_id, _ = await _create_pending_order(client, db, drain_orders, published_event.id)
    events = iter(["evt_a", "evt_b"])
    monkeypatch.setattr(
        "stripe.Webhook.construct_event",
        lambda payload, sig_header, secret: _succeeded_event(order_id, event_id=next(events)),
    )

    for _ in range(2):
        await client.post("/v1/webhooks/stripe", content=b"{}", headers={"Stripe-Signature": "x"})

    assert await db.scalar(select(func.count()).select_from(StripeEvent)) == 2


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
        return {
            "id": "evt_no_order",
            "type": "payment_intent.succeeded",
            "data": {"object": {"metadata": {}}},
        }
    monkeypatch.setattr("stripe.Webhook.construct_event", fake)
    r = await client.post("/v1/webhooks/stripe", content=b"{}", headers={"Stripe-Signature": "x"})
    assert r.status_code == 204


@pytest.mark.asyncio
async def test_webhook_ignores_unknown_order(client, monkeypatch):
    def fake(payload, sig_header, secret):
        return {"id": "evt_unknown_order", "type": "payment_intent.succeeded",
                "data": {"object": {"metadata": {"order_id": "999999"}, "amount_received": 1}}}
    monkeypatch.setattr("stripe.Webhook.construct_event", fake)
    r = await client.post("/v1/webhooks/stripe", content=b"{}", headers={"Stripe-Signature": "x"})
    assert r.status_code == 204


@pytest.mark.asyncio
async def test_unhandled_event_types_are_still_recorded(client, db, monkeypatch):
    """連「我們不處理這種事件」也要記進去重表。

    那也是一種處理結果(決定忽略),而且這張表順便成為「到底收到過哪些事件」的完整
    紀錄 —— 對帳時那份紀錄比 Stripe 的儀表板好用,因為它是**我們這一側**看到的。
    """
    monkeypatch.setattr(
        "stripe.Webhook.construct_event",
        lambda payload, sig_header, secret: {
            "id": "evt_charge_refunded", "type": "charge.refunded", "data": {"object": {}},
        },
    )

    r = await client.post("/v1/webhooks/stripe", content=b"{}", headers={"Stripe-Signature": "x"})

    assert r.status_code == 204
    recorded = await db.scalar(select(StripeEvent.event_type))
    assert recorded == "charge.refunded"


@pytest.mark.asyncio
async def test_seat_is_released_when_payment_is_aborted(client, db, redis, published_event, monkeypatch, drain_orders):
    """付款失敗 → 訂單過期 → **座位真的回到市場**。

    釋放發生在提交之後(和取消/過期同一個模式),所以這條走完整條路由,不是只叫
    handler —— 那個 post-commit 步驟本身就是最容易在重構時掉的東西。
    """
    order_id, _ = await _create_pending_order(client, db, drain_orders, published_event.id)
    before = await get_available(redis, event_id=published_event.id)
    monkeypatch.setattr(
        "stripe.Webhook.construct_event",
        lambda payload, sig_header, secret: {
            "id": "evt_failed",
            "type": "payment_intent.payment_failed",
            "data": {"object": {"id": "pi_test", "metadata": {"order_id": str(order_id)}}},
        },
    )

    r = await client.post("/v1/webhooks/stripe", content=b"{}", headers={"Stripe-Signature": "x"})

    assert r.status_code == 204
    assert await get_available(redis, event_id=published_event.id) == before + 1


# ---------- handler-level:斷言「決定」,不斷言網路 ----------

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

    decision = await _handle_payment_succeeded(db, _intent(order, amount=999))  # underpaid

    assert isinstance(decision, _RefundNeeded)
    assert "999" in decision.reason                   # 理由要帶得走,值班的人靠它判斷
    await db.refresh(order)
    assert order.status == OrderStatus.PENDING        # NOT confirmed


@pytest.mark.asyncio
async def test_handler_refunds_when_order_already_terminal(db, published_event):
    order = await _order(db, published_event, status=OrderStatus.EXPIRED)

    decision = await _handle_payment_succeeded(db, _intent(order))

    assert isinstance(decision, _RefundNeeded)        # 收得太晚 → 退款
    await db.refresh(order)
    assert order.status == OrderStatus.EXPIRED        # unchanged


@pytest.mark.asyncio
async def test_handler_is_a_noop_for_an_already_paid_order(db, published_event):
    order = await _order(db, published_event, status=OrderStatus.CONFIRMED)

    assert await _handle_payment_succeeded(db, _intent(order)) is None
    await db.refresh(order)
    assert order.status == OrderStatus.CONFIRMED


@pytest.mark.asyncio
async def test_handler_aborted_returns_the_order_to_release(db, published_event):
    """handler 只做狀態變更並把「要還座位的那筆訂單」交回去 —— 釋放本身是提交後的
    步驟(它會碰 Redis,而那不該發生在交易裡)。"""
    order = await _order(db, published_event)

    released = await _handle_payment_aborted(db, _intent(order))

    assert released is order
    await db.refresh(order)
    assert order.status == OrderStatus.EXPIRED


@pytest.mark.asyncio
async def test_handler_aborted_skips_an_order_someone_else_moved(db, published_event):
    """已經不是 PENDING 的訂單不能還座位 —— 它的座位屬於把它轉出去的那個寫入者。"""
    order = await _order(db, published_event, status=OrderStatus.CONFIRMED)

    assert await _handle_payment_aborted(db, _intent(order)) is None


# ---------- 去重表的生命週期 ----------

@pytest.mark.asyncio
async def test_old_events_are_purged(db):
    """這張表每一筆對應一次真實付款事件,只增不減會隨營運量無界成長。

    Stripe 最多重送三天,所以保留期只要遠大於那個窗;留久一點純粹是為了對帳。
    """
    now = datetime.now(timezone.utc)
    db.add_all([
        StripeEvent(event_id="evt_old", event_type="x", received_at=now - timedelta(days=40)),
        StripeEvent(event_id="evt_new", event_type="x", received_at=now - timedelta(days=1)),
    ])
    await db.commit()

    deleted = await purge_events_older_than(db, cutoff=cutoff_for(30, now=now))
    await db.commit()

    assert deleted == 1
    assert await db.scalar(select(StripeEvent.event_id)) == "evt_new"
