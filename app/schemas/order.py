from datetime import datetime
from enum import StrEnum
from typing import Annotated
from uuid import UUID

from pydantic import BaseModel, Field, ConfigDict
from app.models.order import OrderStatus


class OrderCreate(BaseModel):
    """Request body for POST /v1/orders."""

    event_id: int
    quantity: Annotated[int, Field(ge=1, le=10)]


class OrderAcceptedResponse(BaseModel):
    """202 response: the order intent was accepted and is being processed.

    The client holds `idempotency_key` as the handle to poll order status.
    """

    idempotency_key: UUID
    status: str = "processing"

class OrderResponse(BaseModel):
    """Order representation sent back to client."""

    model_config = ConfigDict(from_attributes=True)

    id: int
    user_id: int
    event_id: int
    quantity: int
    total_price_cents: int
    status: OrderStatus
    created_at: datetime
    paid_at: datetime | None
    confirmed_at: datetime | None
    expired_at: datetime | None
    cancelled_at: datetime | None


class OrderPage(BaseModel):
    """Keyset-paginated page of a user's orders."""

    items: list[OrderResponse]
    next_cursor: str | None = None   # opaque token; pass back as ?cursor= for the next page


class OrderPollState(StrEnum):
    PROCESSING = "processing"   # accepted, order row not written yet
    READY = "ready"             # order persisted; see `order`
    FAILED = "failed"           # gave up after retries; seat refunded


class OrderStatusResponse(BaseModel):
    """Poll response for GET /orders/by-key/{idempotency_key}."""

    state: OrderPollState
    order: OrderResponse | None = None      # set only when state == READY