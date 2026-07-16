"""Stripe API wrapper service.

Centralizes Stripe SDK calls so the route layer stays Stripe-agnostic.
Uses Stripe's async client (httpx backend) so PaymentIntent creation never
blocks the event loop — the loop natively multiplexes the in-flight HTTPS call.
The client is created once in app lifespan and injected via Depends.
"""
from stripe import StripeClient, HTTPXClient


def create_stripe_client(api_key: str) -> tuple[StripeClient, HTTPXClient]:
    """Build an async Stripe client + its HTTP client.

    Returns both: the StripeClient for requests, and the HTTPXClient so lifespan
    can `await http.close_async()` on shutdown (StripeClient exposes no close).
    """
    http = HTTPXClient()                          # async backend; one pooled connection set
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
