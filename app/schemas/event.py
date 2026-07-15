from datetime import datetime
from typing import Annotated

from pydantic import BaseModel, Field, ConfigDict
from app.models.event import EventStatus


class EventCreate(BaseModel):
    """Request body for POST /v1/events."""
    name: str
    venue: str
    starts_at: datetime
    ends_at: datetime
    sale_starts_at: datetime
    sale_ends_at: datetime
    price_cents: int = Field(ge=0)
    total_seats: int = Field(ge=1)

class EventResponse(BaseModel):
    """Event representation sent back to client."""

    model_config = ConfigDict(from_attributes=True)

    id: int
    name: str
    venue: str
    starts_at: datetime
    ends_at: datetime
    sale_starts_at: datetime
    sale_ends_at: datetime
    price_cents: int
    total_seats: int
    status: EventStatus
    created_at: datetime
    updated_at: datetime


class QueueStatusResponse(BaseModel):
    """Waiting-room state for the current user."""

    admitted: bool
    sold_out: bool = False                 # inventory exhausted — stop waiting, nothing to buy
    paused: bool = False                   # admission frozen by the circuit breaker (downstream unhealthy)
    people_ahead: int | None = None        # not-yet-admitted users ahead (0 = next); None if admitted/unregistered
    poll_after_seconds: int | None = None  # suggested backoff before polling again; None once admitted/sold out
    access_token: str | None = None        # single-use admission pass for POST /orders/; set only when admitted