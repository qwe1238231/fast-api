from datetime import datetime
from typing import Annotated

from pydantic import BaseModel, Field, ConfigDict
from app.models.order import OrderStatus


class OrderCreate(BaseModel):
    """Request body for POST /v1/orders."""

    event_id: int
    quantity: Annotated[int, Field(ge=1, le=10)]

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