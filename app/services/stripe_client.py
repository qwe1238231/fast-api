"""Stripe API wrapper service.

Centralizes Stripe SDK calls so the route layer stays Stripe-agnostic.
Uses Stripe's async client (httpx backend) so PaymentIntent creation never
blocks the event loop — the loop natively multiplexes the in-flight HTTPS call.
The client is created once in app lifespan and injected via Depends.
"""
from typing import Literal

import stripe
from stripe import StripeClient, HTTPXClient

from app.core.config import get_settings

PaymentIntentStatus = Literal[
    "canceled",
    "processing",
    "requires_action",
    "requires_capture",
    "requires_confirmation",
    "requires_payment_method",
    "succeeded",
]
"""Stripe PaymentIntent 的狀態集合。呼叫端要對它做分支,所以用 Literal 讓打錯字在
型別檢查時就被抓到,而不是在 cron 裡靜靜地走到 else。"""

CancellationReason = Literal["abandoned", "duplicate", "fraudulent", "requested_by_customer"]
"""Stripe 接受的取消原因。會出現在儀表板與 intent 物件上 —— 對帳時靠它分出
「T2 超時收掉的」(abandoned)與「使用者自己按取消的」(requested_by_customer)。"""


def create_stripe_client(
    api_key: str, *, timeout_seconds: float | None = None
) -> tuple[StripeClient, HTTPXClient]:
    """Build an async Stripe client + its HTTP client.

    Returns both: the StripeClient for requests, and the HTTPXClient so lifespan
    can `await http.close_async()` on shutdown (StripeClient exposes no close).

    **逾時一定要顯式給。** `HTTPXClient` 的預設是 80 秒,遠長於我們的請求逾時,所以
    永遠是外層先放棄 —— 客戶端拿到 504,而 log 裡沒有任何一行說是 Stripe 慢了。
    webhook 那條路更糟:去重標記已經提交,Stripe 重送會被視為處理過,那筆退款就靜靜
    不見了。設定的 validator 會確保這個值小於請求逾時。
    """
    if timeout_seconds is None:
        timeout_seconds = get_settings().STRIPE_TIMEOUT_SECONDS
    http = HTTPXClient(timeout=timeout_seconds)   # async backend; one pooled connection set
    return StripeClient(api_key, http_client=http), http


async def create_payment_intent(
        client: StripeClient,
        *,
        amount: int,
        currency: str,
        order_id: int,
) -> dict[str, str]:
    """Create a Stripe PaymentIntent for an order.

    Returns dict with `id` and `client_secret`. Non-blocking (async HTTP).
    """
    intent = await client.v1.payment_intents.create_async(
        {
            "amount": amount,
            "currency": currency,
            "metadata": {"order_id": str(order_id)},
        },
        {"idempotency_key": f"order-{order_id}"},   # idempotency_key lives in options
    )
    return {"id": intent.id, "client_secret": intent.client_secret}


async def create_refund(
        client: StripeClient,
        *,
        payment_intent_id: str,
) -> dict[str, str]:
    """Refund a PaymentIntent in full — used when a charge lands on an order that
    is no longer payable (expired/cancelled after payment) or the captured amount
    doesn't match. Idempotent per intent, so a re-delivered webhook won't
    double-refund. Non-blocking (async HTTP).
    """
    refund = await client.v1.refunds.create_async(
        {"payment_intent": payment_intent_id},
        {"idempotency_key": f"refund-{payment_intent_id}"},
    )
    return {"id": refund.id, "status": refund.status}


async def cancel_payment_intent(
        client: StripeClient,
        *,
        payment_intent_id: str,
        reason: CancellationReason = "abandoned",
) -> PaymentIntentStatus:
    """Cancel a PaymentIntent. Returns Stripe's status for it **afterwards**.

    回傳狀態而不是 bool,因為呼叫端(在途付款的超時 cron、使用者取消訂單)要的不是
    「取消成功了嗎」,是「**Stripe 那邊現在是什麼狀態**」—— 而後續動作取決於它:

      canceled    → 剛取消成功,或本來就取消了。不會再有錢進來,本地可以 EXPIRED。
      succeeded   → 錢已經到了,只是 `succeeded` webhook 還沒進來。**絕不能** EXPIRED。
      其他        → 還在流程中(processing / requires_action …),Stripe 拒絕取消。
                    這一輪不動,下一輪再來。

    `payment_intent_unexpected_state` 是「這個狀態不能取消」,錯誤體**自帶**當下的
    intent,所以先從那裡讀,讀不到才退回 retrieve —— 常見失敗路徑只花一次往返。
    其他 InvalidRequestError(例如 `resource_missing`)原樣拋出:那不是狀態問題,
    是資料問題,呼叫端要看見。

    `reason` 的意義見 CancellationReason。預設 abandoned 是因為第一個呼叫端是 T2 cron;
    使用者按取消的路徑要明確傳 requested_by_customer,不然對帳時兩者混在一起。
    """
    try:
        intent = await client.v1.payment_intents.cancel_async(
            payment_intent_id, {"cancellation_reason": reason}
        )
    except stripe.InvalidRequestError as exc:
        if exc.code != "payment_intent_unexpected_state":
            raise
        carried = getattr(exc.error, "payment_intent", None)
        status = getattr(carried, "status", None)
        if status is None:
            intent = await client.v1.payment_intents.retrieve_async(payment_intent_id)
            status = intent.status
        return status
    return intent.status
