"""Stripe webhook receiver — drives the payment side of the order lifecycle.

**這一支的結構是「交易內決定、提交後動錢」,不是隨手排的:**

    1. 驗簽
    2. 同一個交易裡:去重 claim + 訂單狀態變更
    3. 提交
    4. 提交之後才碰外部(退款、還座位)—— 全部冪等

為什麼退款不能在交易裡:那是一次外部網路往返,而交易期間會抓著一條 pooled 連線
與 vacuum 的 xmin horizon。也為什麼不能在提交**之前**:提交失敗就會變成「退了款但
訂單沒動」。

第 2 步的 claim 讓重送成為 no-op,而它同時是並發重送的**互斥鎖** —— 見
`models/stripe_event.py` 裡那個「同一個人拿到票又拿到退款」的競態。
"""
import logging
from dataclasses import dataclass
from typing import Annotated

import stripe
from fastapi import APIRouter, Header, HTTPException, Request, status

from app.api.deps import DbSession, Redis, Stripe
from app.core.config import get_settings
from app.core.logging import alert
from app.crud.order import get_order_by_id
from app.crud.stripe_event import claim_event
from app.models.order import Order, OrderStatus
from app.services.orders import mark_confirmed, mark_paid, expire_order, release_order_seat
from app.services.stripe_client import create_refund

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/webhooks", tags=["webhooks"])


@dataclass(frozen=True, slots=True)
class _RefundNeeded:
    """「這筆錢不能收」的決定。決定在交易內做,動作在提交後做。"""

    payment_intent_id: str
    reason: str


def _order_id(intent: dict) -> int | None:
    try:
        return int(intent["metadata"]["order_id"])
    except (KeyError, TypeError, ValueError):
        return None


@router.post("/stripe", status_code=status.HTTP_204_NO_CONTENT)
async def stripe_webhook(
    request: Request,
    stripe_signature: Annotated[str, Header(alias="Stripe-Signature")],
    db: DbSession,
    stripe_client: Stripe,
    redis: Redis,
) -> None:
    """Receive Stripe events. Signature-verified, de-duplicated, handlers idempotent."""
    settings = get_settings()
    raw_body = await request.body()

    try:
        event = stripe.Webhook.construct_event(
            payload=raw_body,
            sig_header=stripe_signature,
            secret=settings.STRIPE_WEBHOOK_SECRET,
        )
    except stripe.SignatureVerificationError:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Invalid signature",
        )

    etype = event["type"]
    # 去重在**做任何事之前**。連「這個事件我們不處理」也要 claim:那也是一種處理結果,
    # 而且這張表順便成為「收到過哪些事件」的完整紀錄。
    if not await claim_event(db, event_id=event["id"], event_type=etype):
        logger.info(
            "duplicate stripe event ignored",
            extra={
                "event": "stripe_event_duplicate",
                "stripe_event_id": event["id"],
                "stripe_event_type": etype,
            },
        )
        return

    intent = event["data"]["object"]
    refund: _RefundNeeded | None = None
    release: Order | None = None
    if etype == "payment_intent.succeeded":
        refund = await _handle_payment_succeeded(db, intent)
    elif etype in ("payment_intent.payment_failed", "payment_intent.canceled"):
        release = await _handle_payment_aborted(db, intent)

    # claim 與狀態變更一起提交。這裡拋例外的話兩者一起回滾,而 Stripe 的重送會被當成
    # 新事件重跑 —— 「失敗要能重試」是同一個交易免費附帶的性質。
    await db.commit()

    if release is not None:
        try:
            await release_order_seat(db, redis, release)
        except Exception:  # release 是冪等的;失敗只是暫時弄丟一個座位
            alert(
                logger,
                "payment aborted: order expired but seat release failed",
                event="seat_release_failed",
                exc_info=True,
                order_id=release.id,
                event_id=release.event_id,
            )

    if refund is not None:
        # 真的有錢要退出去。冪等鍵是 `refund-{intent}`,所以重跑安全 —— 但這裡失敗
        # **沒有自動重試**:退款是罕見且涉及金額的事,寧可要一個人來看,也不要一個
        # 自動迴圈在錯誤的前提下反覆送。
        alert(
            logger,
            "refunding a charge we cannot honour",
            event="payment_refunded",
            payment_intent_id=refund.payment_intent_id,
            reason=refund.reason,
        )
        try:
            await create_refund(
                stripe_client, payment_intent_id=refund.payment_intent_id
            )
        except Exception:
            alert(
                logger,
                "refund call failed — the charge is still held; refund it by hand",
                event="refund_failed",
                exc_info=True,
                payment_intent_id=refund.payment_intent_id,
            )


async def _handle_payment_succeeded(db, intent: dict) -> _RefundNeeded | None:
    """A charge landed. Confirm the order iff it's still payable AND the amount is
    right; otherwise ask for a refund (charged too late / wrong amount).

    **只碰資料庫,不碰網路。** 回傳「要退款」這個決定,由呼叫端在提交之後執行。
    """
    order_id = _order_id(intent)
    if order_id is None:
        return None
    order = await get_order_by_id(db, order_id)
    if order is None:
        return None

    captured = intent.get("amount_received") or intent.get("amount")

    # (1) 金額必須等於訂單的金額 —— 不然退款,絕不確認。
    if captured != order.total_price_cents:
        return _RefundNeeded(
            payment_intent_id=intent["id"],
            reason=f"amount {captured} != order total {order.total_price_cents}",
        )

    # (2) 已經 paid/confirmed —— 走到這裡代表 claim 是新的(不同事件),但訂單早就
    #     成交了(例如先前那次是別的 intent)。什麼都不做。
    if order.status in (OrderStatus.PAID, OrderStatus.CONFIRMED):
        return None

    # (3) 訂單已經不可付款(付款途中被取消/過期)—— 退款。
    if order.status != OrderStatus.PENDING:
        return _RefundNeeded(
            payment_intent_id=intent["id"],
            reason=f"order {order_id} is {order.status.value}",
        )

    # (4) PENDING:收下。CAS 失敗代表**同一瞬間**有別的寫入者把它移出 PENDING;
    #     那是真正的競態(例如過期 cron 剛好跑到),不是重送 —— 重送在上面就被
    #     claim 擋掉了。
    if not await mark_paid(db, order):
        return _RefundNeeded(
            payment_intent_id=intent["id"],
            reason=f"order {order_id} left PENDING mid-payment",
        )
    await mark_confirmed(db, order)
    return None


async def _handle_payment_aborted(db, intent: dict) -> Order | None:
    """Payment failed or the intent was canceled/abandoned -> expire the order (it
    was protected from the timeout cron while in flight).

    回傳需要在提交後還座位的那筆訂單;沒有就 None。
    """
    order_id = _order_id(intent)
    if order_id is None:
        return None
    order = await get_order_by_id(db, order_id)
    if order is None or order.status != OrderStatus.PENDING:
        return None  # only a still-PENDING order is holding a seat to release
    if not await expire_order(db, order):
        return None  # someone else already transitioned it
    return order
