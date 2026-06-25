from typing import Annotated
from uuid import UUID

from fastapi import APIRouter, Header, status, HTTPException

from app.api.deps import CurrentUser, DbSession, Redis
from app.models.order import Order, OrderStatus
from app.schemas.order import OrderCreate, OrderResponse, OrderAcceptedResponse, OrderStatusResponse, OrderPollState
from app.services.orders import submit_order, cancel_order as cancel_order_service, mark_confirmed, mark_paid
from app.services.idempotency import get_claim_state, CLAIM_PENDING, CLAIM_FAILED
from app.core.exceptions import OrderNotFound, InvalidOrderTransition
from app.crud.order import get_order_by_id, get_order_by_idempotency_key, list_orders_for_user
from app.schemas.payment import PaymentIntentResponse
from app.services.stripe_client import create_payment_intent


router = APIRouter(prefix="/orders", tags=["orders"])


@router.post(
    "/",
    response_model=OrderAcceptedResponse,
    status_code=status.HTTP_202_ACCEPTED,
)
async def create_endpoint(
    order_in: OrderCreate,
    idempotency_key: Annotated[UUID, Header(alias="Idempotency-Key")],
    current_user: CurrentUser,
    db: DbSession,
    redis: Redis,
) -> OrderAcceptedResponse:
    """Accept an order intent: validate, reserve a seat, enqueue for the worker.

    Returns 202 immediately — the order row is written asynchronously by the
    worker. The client polls order status with the Idempotency-Key.
    """
    await submit_order(
        db,
        redis,
        user_id=current_user.id,
        event_id=order_in.event_id,
        quantity=order_in.quantity,
        idempotency_key=idempotency_key,
    )
    return OrderAcceptedResponse(idempotency_key=idempotency_key)

@router.get("/me", response_model=list[OrderResponse])
async def list_my_orders(
    current_user: CurrentUser,
    db: DbSession,
) -> list[Order]:
    """List orders belonging to the authenticated user, newest first."""
    return await list_orders_for_user(db, current_user.id)

@router.get("/by-key/{idempotency_key}", response_model=OrderStatusResponse)
async def get_order_status(
    idempotency_key: UUID,
    current_user: CurrentUser,
    db: DbSession,
    redis: Redis,
) -> OrderStatusResponse:
    """Poll an order's status by the Idempotency-Key used to create it.

    Persisted (and owned) -> the order. Still in the queue -> processing.
    Otherwise 404 (also when the key belongs to another user — don't leak).
    """
    order = await get_order_by_idempotency_key(db, idempotency_key)
    if order is not None and order.user_id == current_user.id:
        return OrderStatusResponse(
            state=OrderPollState.READY,
            order=OrderResponse.model_validate(order),
        )
    if order is None:
        claim = await get_claim_state(redis, idempotency_key=str(idempotency_key))
        if claim == CLAIM_PENDING:
            return OrderStatusResponse(state=OrderPollState.PROCESSING)
        if claim == CLAIM_FAILED:
            return OrderStatusResponse(state=OrderPollState.FAILED)
    raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="No order for this key")


@router.get("/{order_id}", response_model=OrderResponse)
async def get_order(
    order_id: int,
    current_user: CurrentUser,
    db: DbSession, 
) -> Order:
    """Get a single order. Returns 404 if not found or not owned by current user."""
    order = await get_order_by_id(db, order_id)
    if order is None or order.user_id != current_user.id:
        raise OrderNotFound(order_id=order_id)
    return order

@router.post("/{order_id}/pay", status_code=status.HTTP_204_NO_CONTENT)
async def pay_order(
    order_id: int,
    current_user: CurrentUser,
    db: DbSession,
) -> None:
    """Mock payment: PENDING → PAID → CONFIRMED in one shot."""
    order = await get_order_by_id(db, order_id)
    if order is None or order.user_id != current_user.id:
        raise OrderNotFound(order_id=order_id)
    
    await mark_paid(db, order)
    await mark_confirmed(db, order)

    await db.commit()

@router.post("/{order_id}/cancel", status_code=status.HTTP_204_NO_CONTENT)
async def cancel_order(
    order_id: int,
    current_user: CurrentUser,
    db: DbSession,
    redis: Redis,
) -> None:
    """Cancel an order. Releases reserved inventory."""
    order = await get_order_by_id(db, order_id)
    if order is None or order.user_id != current_user.id:
        raise OrderNotFound (order_id=order_id)
    
    await cancel_order_service(db, redis, order)
    await db.commit()

@router.post("/{order_id}/payment-intent", response_model=PaymentIntentResponse)
async def create_order_payment_intent(
        order_id: int,
        current_user: CurrentUser,
        db: DbSession,
) -> PaymentIntentResponse:
    """Create a Stripe PaymentIntent for the given order.
    
    Returns client_secret for the frontend to complete payment.
    """
    order = await get_order_by_id(db, order_id)
    if order is None or order.user_id != current_user.id:
        raise OrderNotFound(order_id=order_id)
    
    if order.status != OrderStatus.PENDING:
        raise InvalidOrderTransition(
            order_id=order_id,
            from_status=order.status.value,
            to_status="paying",
        )
    
    intent = create_payment_intent(
        amount=order.total_price_cents,
        currency="usd",
        order_id=order.id,
    )

    order.payment_provider_id = intent["id"]
    await db.commit()
    
    return PaymentIntentResponse(
        payment_intent_id=intent["id"],
        client_secret=intent["client_secret"],
    )