from typing import Annotated
from uuid import UUID

from fastapi import APIRouter, Header, status

from app.api.deps import CurrentUser, DbSession, Redis
from app.models.order import Order
from app.schemas.order import OrderCreate, OrderResponse
from app.services.orders import create_order_with_inventory, cancel_order as cancel_order_service, mark_confirmed, mark_paid
from app.core.exceptions import OrderNotFound
from app.crud.order import get_order_by_id, list_orders_for_user

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