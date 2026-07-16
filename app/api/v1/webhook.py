"""Stripe webhook receiver — drives the payment side of the order lifecycle."""
from typing import Annotated

import stripe
from fastapi import APIRouter, Header, HTTPException, Request, status

from app.api.deps import DbSession, Redis, Stripe
from app.core.config import get_settings
from app.crud.order import get_order_by_id
from app.models.order import OrderStatus
from app.services.orders import mark_confirmed, mark_paid, expire_order, release_order_seat
from app.services.stripe_client import create_refund

router = APIRouter(prefix="/webhooks", tags=["webhooks"])


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
    """Receive Stripe events. Signature-verified; handlers are idempotent."""
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
    intent = event["data"]["object"]
    if etype == "payment_intent.succeeded":
        await _handle_payment_succeeded(db, stripe_client, intent)
    elif etype in ("payment_intent.payment_failed", "payment_intent.canceled"):
        await _handle_payment_aborted(db, redis, intent)


async def _refund(stripe_client, intent: dict, *, reason: str) -> None:
    """Refund a charge we can't honour. Logged for ops; idempotent per intent."""
    print(f"REFUND payment_intent {intent.get('id')} — {reason}")
    await create_refund(stripe_client, payment_intent_id=intent["id"])


async def _handle_payment_succeeded(db, stripe_client, intent: dict) -> None:
    """A charge landed. Confirm the order iff it's still payable AND the amount is
    right; otherwise refund (charged too late / wrong amount) or no-op (duplicate)."""
    order_id = _order_id(intent)
    if order_id is None:
        return
    order = await get_order_by_id(db, order_id)
    if order is None:
        return

    # (1) amount must equal what the order costs — else refund, never confirm.
    captured = intent.get("amount_received") or intent.get("amount")
    if captured != order.total_price_cents:
        await _refund(stripe_client, intent,
                      reason=f"amount {captured} != order total {order.total_price_cents}")
        return

    # (2) already paid/confirmed (Stripe re-delivered the event) — idempotent no-op.
    if order.status in (OrderStatus.PAID, OrderStatus.CONFIRMED):
        return

    # (3) order is no longer payable (e.g. cancelled while paying) — refund.
    if order.status != OrderStatus.PENDING:
        await _refund(stripe_client, intent, reason=f"order {order_id} is {order.status.value}")
        return

    # (4) PENDING: pay it. A concurrent transition makes the CAS miss -> refund.
    if not await mark_paid(db, order):
        await db.rollback()
        await _refund(stripe_client, intent, reason=f"order {order_id} left PENDING mid-payment")
        return
    await mark_confirmed(db, order)
    await db.commit()


async def _handle_payment_aborted(db, redis, intent: dict) -> None:
    """Payment failed or the intent was canceled/abandoned -> release the seat and
    expire the order (it was protected from the timeout cron while in flight)."""
    order_id = _order_id(intent)
    if order_id is None:
        return
    order = await get_order_by_id(db, order_id)
    if order is None or order.status != OrderStatus.PENDING:
        return  # only a still-PENDING order is holding a seat to release
    if not await expire_order(db, order):
        return  # someone else already transitioned it
    await db.commit()
    try:
        await release_order_seat(redis, order)
    except Exception as exc:  # release is idempotent; a blip is a recoverable lost seat
        print(f"payment aborted for order {order_id}; expired but seat release failed: {exc}")
