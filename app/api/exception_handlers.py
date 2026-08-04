"""Translate domain exceptions to HTTP responses.

Domain layer raises business concepts (EventNotFound, InsufficientInventory etc.).
This module is the only place that knows how each maps to HTTP status + body.
"""
from fastapi import FastAPI, Request, status
from fastapi.responses import JSONResponse

from app.core.exceptions import (
    DomainError,
    DuplicateOrderRequest,
    EventCancelled,
    EventNotFound,
    EventNotOnSale,
    InsufficientInventory,
    InvalidOrderTransition,
    OrderNotFound,
    OrderNotOwned,
    AdmissionDenied,
    RateLimited,
    BuyerInfoAlreadyExists,
    BuyerInfoNotFound,
    NationalIdAlreadyRegistered,
    ZoneNotForEvent,
    ZoneRequired,
)

def register_exception_handlers(app: FastAPI) -> None:
    """Wire all domain → HTTP translators into the FastAPI app."""

    @app.exception_handler(EventNotFound)
    async def _event_not_found(request: Request, exc: EventNotFound):
        return JSONResponse(
            status_code=status.HTTP_404_NOT_FOUND,
            content={"detail": str(exc), "event_id": exc.event_id},
        )
    
    @app.exception_handler(EventCancelled)
    async def _event_cancelled(request: Request, exc: EventCancelled):
        return JSONResponse(
            status_code=status.HTTP_409_CONFLICT,
            content={"detail": str(exc), "event_id": exc.event_id},
        )
    # 兩種 zone 錯誤都是「請求帶錯了」而不是「狀態衝突」,所以 422 而非 409。
    @app.exception_handler(ZoneRequired)
    async def _zone_required(request: Request, exc: ZoneRequired):
        return JSONResponse(
            status_code=status.HTTP_422_UNPROCESSABLE_CONTENT,
            content={"detail": str(exc), "event_id": exc.event_id},
        )

    @app.exception_handler(ZoneNotForEvent)
    async def _zone_not_for_event(request: Request, exc: ZoneNotForEvent):
        # 只回泛用訊息,不回 exc.reason —— 別讓外部藉由錯誤差異探測場館結構。
        return JSONResponse(
            status_code=status.HTTP_422_UNPROCESSABLE_CONTENT,
            content={
                "detail": f"Zone {exc.zone_id} is not available for event {exc.event_id}",
                "event_id": exc.event_id,
                "zone_id": exc.zone_id,
            },
        )

    @app.exception_handler(EventNotOnSale)
    async def _event_not_on_sale(request: Request, exc: EventNotOnSale):
        return JSONResponse(
            status_code=status.HTTP_409_CONFLICT,
            content={"detail": str(exc), "event_id": exc.event_id},
        )
    @app.exception_handler(OrderNotFound)
    async def _order_not_found(request: Request, exc: OrderNotFound):
        return JSONResponse(
            status_code=status.HTTP_404_NOT_FOUND,
            content={"detail": str(exc), "order_id": exc.order_id},
        )
    @app.exception_handler(OrderNotOwned)
    async def _order_not_owned(request: Request, exc: OrderNotOwned):
        return JSONResponse(
            status_code=status.HTTP_403_FORBIDDEN,
            content={"detail": str(exc), "order_id": exc.order_id},
        )
    @app.exception_handler(InvalidOrderTransition)
    async def _invalid_order_transition(request: Request, exc: InvalidOrderTransition):
        return JSONResponse(
            status_code=status.HTTP_409_CONFLICT,
            content={
                "detail": str(exc),
                "order_id": exc.order_id,
                "from_status": exc.from_status,
                "to_status": exc.to_status,
            },
        )
    @app.exception_handler(DuplicateOrderRequest)
    async def _duplicate_order_request(request: Request, exc: DuplicateOrderRequest):
        return JSONResponse(
            status_code=status.HTTP_409_CONFLICT,
            content={
                "detail": str(exc),
                "idempotency_key": exc.idempotency_key,
            },
        )
    @app.exception_handler(InsufficientInventory)
    async def _insufficient_inventory(request: Request, exc: InsufficientInventory):    
        return JSONResponse(
            status_code=status.HTTP_409_CONFLICT,
            content={
                "detail": str(exc),
                "event_id": exc.event_id,
                "requested": exc.requested,
                "available": exc.available,
            },
        )
    
    @app.exception_handler(BuyerInfoNotFound)
    async def _buyer_info_not_found(request: Request, exc: BuyerInfoNotFound):
        return JSONResponse(
            status_code=status.HTTP_404_NOT_FOUND,
            content={"detail": str(exc), "user_id": exc.user_id},
        )

    @app.exception_handler(BuyerInfoAlreadyExists)
    async def _buyer_info_already_exists(request: Request, exc: BuyerInfoAlreadyExists):
        return JSONResponse(
            status_code=status.HTTP_409_CONFLICT,
            content={"detail": str(exc), "user_id": exc.user_id},
        )

    @app.exception_handler(NationalIdAlreadyRegistered)
    async def _national_id_already_registered(request: Request, exc:NationalIdAlreadyRegistered):
        return JSONResponse(
            status_code=status.HTTP_409_CONFLICT,
            content={"detail": str(exc)},
        )

    @app.exception_handler(AdmissionDenied)
    async def _admission_denied(request: Request, exc: AdmissionDenied):
        return JSONResponse(
            status_code=status.HTTP_403_FORBIDDEN,
            content={"detail": str(exc)},
        )

    @app.exception_handler(RateLimited)
    async def _rate_limited(request: Request, exc: RateLimited):
        headers = {"Retry-After": str(exc.retry_after)} if exc.retry_after else None
        return JSONResponse(
            status_code=status.HTTP_429_TOO_MANY_REQUESTS,
            content={"detail": str(exc)},
            headers=headers,
        )

    @app.exception_handler(DomainError)
    async def _domain_error(request: Request, exc: DomainError):
        return JSONResponse(
            status_code=status.HTTP_400_BAD_REQUEST,
            content={"detail": str(exc)},
        )