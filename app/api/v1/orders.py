import base64
from datetime import datetime
from typing import Annotated
from uuid import UUID

from fastapi import APIRouter, Header, status, HTTPException, Query

from app.api.deps import CurrentUser, DbSession, Redis, Stripe
from app.models.order import Order, OrderStatus
from app.schemas.order import OrderCreate, OrderResponse, OrderAcceptedResponse, OrderStatusResponse, OrderPollState, OrderPage
from app.services.orders import submit_order, cancel_order as cancel_order_service, mark_confirmed, mark_paid, release_order_seat
from app.services.idempotency import get_claim_state, CLAIM_PENDING, CLAIM_FAILED
from app.core.config import get_settings
from app.core.exceptions import DomainError, OrderNotFound, InvalidOrderTransition, SeatsNotAssigned
from app.crud.order import get_order_by_id, get_order_by_idempotency_key, list_orders_for_user
from app.schemas.payment import PaymentIntentResponse
from app.schemas.seating import SeatedOrderDetail
from app.services.zones import describe_order_seats
from app.services.stripe_client import create_payment_intent
from app.services.waiting_room import refund_admission, verify_admission


router = APIRouter(prefix="/orders", tags=["orders"])


@router.post(
    "/",
    response_model=OrderAcceptedResponse,
    status_code=status.HTTP_202_ACCEPTED,
)
async def create_endpoint(
    order_in: OrderCreate,
    idempotency_key: Annotated[UUID, Header(alias="Idempotency-Key")],
    admission_token: Annotated[str, Header(alias="Admission-Token")],
    current_user: CurrentUser,
    db: DbSession,
    redis: Redis,
) -> OrderAcceptedResponse:
    """Accept an order intent: validate, reserve a seat, enqueue for the worker.

    Requires a valid waiting-room admission token for this event (checked before
    any reserve work, so non-admitted requests are bounced cheaply). Returns 202
    immediately — the order row is written asynchronously by the worker.
    """
    # Bypass is guarded in Settings (only honoured under DEBUG); in prod this
    # branch is dead and every order goes through admission verification.
    jti: str | None = None
    if not get_settings().LOADTEST_BYPASS_ADMISSION:
        jti = await verify_admission(
            redis, admission_token, user_id=current_user.id, event_id=order_in.event_id
        )
    try:
        await submit_order(
            db,
            redis,
            user_id=current_user.id,
            event_id=order_in.event_id,
            quantity=order_in.quantity,
            zone_id=order_in.zone_id,
            idempotency_key=idempotency_key,
        )
    except DomainError:
        # 沒有任何訂單意圖成立 → 把單次入場券還回去,讓使用者能改張數/改區重試,
        # 而不是被迫重新排隊。不變式是「入場券被消耗 ⟺ 訂單意圖已受理」。
        #
        # 只攔 DomainError:那些都是在 reserve 之前(或 reserve 回報 SOLD_OUT 而
        # 沒有實際扣庫存時)拋出的。非預期的例外有可能發生在 reserve 成功之後,
        # 那時退還 token 就會讓一張券換到兩張票 —— 寧可弄丟一張券,也不要重複售出。
        if jti is not None:
            await refund_admission(redis, jti)
        raise
    return OrderAcceptedResponse(idempotency_key=idempotency_key)

def _encode_cursor(order: Order) -> str:
    """Opaque keyset cursor from an order's (created_at, id). Clients pass it back verbatim."""
    raw = f"{order.created_at.isoformat()}|{order.id}"
    return base64.urlsafe_b64encode(raw.encode()).decode()


def _decode_cursor(token: str) -> tuple[datetime, int]:
    try:
        raw = base64.urlsafe_b64decode(token.encode()).decode()
        created_at_str, id_str = raw.rsplit("|", 1)
        return datetime.fromisoformat(created_at_str), int(id_str)
    except (ValueError, TypeError):
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY, detail="invalid cursor"
        )


@router.get("/me", response_model=OrderPage)
async def list_my_orders(
    current_user: CurrentUser,
    db: DbSession,
    limit: Annotated[int, Query(ge=1, le=100)] = 50,
    cursor: str | None = None,
) -> OrderPage:
    """List the authenticated user's orders, newest first (keyset pagination).

    Pass the previous response's `next_cursor` back as `?cursor=` for the next page.
    """
    decoded = _decode_cursor(cursor) if cursor else None
    # fetch one extra to detect a next page without a COUNT
    rows = await list_orders_for_user(db, current_user.id, limit=limit + 1, cursor=decoded)
    has_more = len(rows) > limit
    page = rows[:limit]
    next_cursor = _encode_cursor(page[-1]) if has_more else None
    return OrderPage(
        items=[OrderResponse.model_validate(o) for o in page],
        next_cursor=next_cursor,
    )

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

@router.get("/{order_id}/seats", response_model=SeatedOrderDetail)
async def get_order_seats(
    order_id: int,
    current_user: CurrentUser,
    db: DbSession,
) -> SeatedOrderDetail:
    """已確認訂單的座號。

    確認之前刻意 409 而不是回空值:座號在那之前還可能被 compaction 移動,吐出去
    就等於凍結它。回一個明確的「尚未公開」比回 null 好 —— 後者跟「這場沒有座位圖」
    分不出來。
    """
    order = await get_order_by_id(db, order_id)
    if order is None or order.user_id != current_user.id:
        raise OrderNotFound(order_id=order_id)
    if order.zone_id is None or order.status != OrderStatus.CONFIRMED:
        raise SeatsNotAssigned(order_id=order_id)
    return await describe_order_seats(db, order)


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
    
    # CAS PENDING->PAID: False means the order left PENDING (e.g. the expire cron
    # got it first) -> 409, do NOT proceed. This is the /pay half of the
    # expire-vs-pay race; the CAS in transition_order_status makes the two writers
    # serialize instead of blindly overwriting each other.
    if not await mark_paid(db, order):
        await db.refresh(order)   # read the real current status for the 409 detail
        raise InvalidOrderTransition(
            order_id=order_id, from_status=order.status.value, to_status="paid",
        )
    await mark_confirmed(db, order)   # same txn: our own PAID is visible -> applies
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
    
    if not await cancel_order_service(db, order):
        await db.refresh(order)
        raise InvalidOrderTransition(
            order_id=order_id, from_status=order.status.value, to_status="cancelled",
        )
    await db.commit()
    # post-commit, idempotent seat return; a failure here is a recoverable lost
    # seat (reconcile), NOT a failed cancel -> log, don't 500 the client.
    try:
        await release_order_seat(db, redis, order)
    except Exception as exc:
        print(f"Order {order_id} cancelled but seat release failed (reconcile recovers): {exc}")

@router.post("/{order_id}/payment-intent", response_model=PaymentIntentResponse)
async def create_order_payment_intent(
        order_id: int,
        current_user: CurrentUser,
        db: DbSession,
        stripe: Stripe,
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
    
    intent = await create_payment_intent(
        stripe,
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