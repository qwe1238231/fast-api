from typing import Annotated
from uuid import UUID

from fastapi import APIRouter, Header, status

from app.api.deps import CurrentUser, DbSession, Redis
from app.models.order import Order, OrderStatus
from app.schemas.order import OrderCreate, OrderResponse
from app.services.orders import create_order_with_inventory, cancel_order as cancel_order_service, mark_confirmed, mark_paid
from app.core.exceptions import OrderNotFound, InvalidOrderTransition
from app.crud.order import get_order_by_id, list_orders_for_user
from app.schemas.payment import PaymentIntentResponse
from app.services.stripe_client import create_payment_intent


router = APIRouter(prefix="/orders", tags=["orders"])


@router.post(
    "/",
    response_model=OrderResponse,
    status_code=status.HTTP_201_CREATED,
)
async def create_endpoint(
    order_in: OrderCreate,
    idempotency_key: Annotated[UUID, Header(alias="Idempotency-Key")],
    current_user: CurrentUser,
    db: DbSession,
    redis: Redis,
) -> Order:
    """Create a new pending order."""
    order = await create_order_with_inventory(
        db,
        redis,
        user_id=current_user.id,
        event_id=order_in.event_id,
        quantity=order_in.quantity,
        idempotency_key=idempotency_key,
    )
    await db.commit()
    return order

@router.get("/me", response_model=list[OrderResponse])
async def list_my_orders(
    current_user: CurrentUser,
    db: DbSession,
) -> list[Order]:
    """List orders belonging to the authenticated user, newest first."""
    return await list_orders_for_user(db, current_user.id)

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