"""Stripe API wrapper service.

Centralizes Stripe SDK calls so the route layer stays Stripe-agnostic.
Uses Stripe's async client (httpx backend) so PaymentIntent creation never
blocks the event loop — the loop natively multiplexes the in-flight HTTPS call.
The client is created once in app lifespan and injected via Depends.
"""
from stripe import StripeClient, HTTPXClient

from app.core.config import get_settings


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
