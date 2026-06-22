"""Stripe API wrapper service.

Centralizes Stripe SDK calls so route layer stays Stripe-agnostic.
"""
import stripe

from app.core.config import get_settings

def _ensure_initialized() -> None:
    """Set Stripe API key from settings. Safe to call multiple times."""
    stripe.api_key = get_settings().STRIPE_SECRET_KEY

def create_payment_intent(
        *,
        amount: int,
        currency: str,
        order_id: int,
) -> dict[str, str]:
    """Create a Stripe PaymentIntent for an order.
    
    Returns dict with `id` and `client_secret`.
    """
    _ensure_initialized()

    intent = stripe.PaymentIntent.create(
        amount=amount,
        currency=currency,
        metadata={"order_id": str(order_id)},
        idempotency_key=f"order-{order_id}",
    )

    return {"id": intent.id, "client_secret": intent.client_secret}