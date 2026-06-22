"""Stripe webhook receiver."""
from typing import Annotated

import stripe
from fastapi import APIRouter, Header, HTTPException, Request, status

from app.api.deps import DbSession
from app.core.config import get_settings
from app.core.exceptions import InvalidOrderTransition
from app.crud.order import get_order_by_id
from app.services.orders import mark_confirmed, mark_paid

router = APIRouter(prefix="/webhooks", tags=["webhooks"])


@router.post("/stripe", status_code=status.HTTP_204_NO_CONTENT)
async def stripe_webhook(
    request: Request,
    stripe_signature: Annotated[str, Header(alias="Stripe-Signature")],
    db: DbSession,
) -> None:
    """Receive Stripe events. Signature verified; idempotent."""
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
    
    if event["type"] == "payment_intent.succeeded":
        await _handle_payment_succeeded(db, event["data"]["object"])

async def _handle_payment_succeeded(db, intent:dict) -> None:
    """Mark the corresponding order as paid + confirmed."""
    try:
        order_id_str = intent["metadata"]["order_id"]
    except (KeyError, TypeError):
        return
    
    order = await get_order_by_id(db, int(order_id_str))
    if order is None:
        return
    
    try:
        await mark_paid(db, order)
        await mark_confirmed(db, order)
        await db.commit()
    except InvalidOrderTransition:
        await db.rollback()
    