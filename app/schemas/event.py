from datetime import datetime
from typing import Annotated

from pydantic import BaseModel, Field, ConfigDict, model_validator
from app.models.event import EventStatus


class EventCreate(BaseModel):
    """Request body for POST /v1/events。

    **兩種場次的必填欄位不同**,所以用 validator 把形狀說清楚而不是塞一堆
    optional 進去讓呼叫端猜:

      無座位圖(venue_id 省略)   必填 price_cents + total_seats
      有座位圖(venue_id 給定)   必填 zone_prices;price_cents 與 total_seats
                                **不可給** —— total_seats 是 Σ block 容量的衍生值,
                                要求管理員手打再驗證只是在製造 SeatMapMismatch。
    """

    name: str
    venue: str
    starts_at: datetime
    ends_at: datetime
    sale_starts_at: datetime
    sale_ends_at: datetime

    venue_id: int | None = None
    """給了就是座位場次。座位圖屬於場館,佔用屬於場次。"""

    price_cents: int | None = Field(default=None, ge=0)
    total_seats: int | None = Field(default=None, ge=1)

    zone_prices: dict[int, int] | None = None
    """zone_id → 單價(分)。座位場次必填,而且必須**涵蓋該場館的每一個 zone** ——
    少一個的話那一區的容量會算進 total_seats 卻永遠賣不掉,場次就永遠賣不完、
    等候室的 sold_out 永不觸發(見 ZonePricesIncomplete)。"""

    @model_validator(mode="after")
    def _check_shape(self) -> "EventCreate":
        if self.venue_id is None:
            if self.price_cents is None or self.total_seats is None:
                raise ValueError(
                    "無座位圖的場次必須提供 price_cents 與 total_seats"
                )
            if self.zone_prices:
                raise ValueError("zone_prices 需要 venue_id —— 沒有座位圖就沒有區")
        else:
            if not self.zone_prices:
                raise ValueError("座位場次必須提供 zone_prices(每一個 zone 都要)")
            if self.price_cents is not None or self.total_seats is not None:
                raise ValueError(
                    "座位場次不要提供 price_cents / total_seats:"
                    "票價按區、總席數由座位圖容量推導"
                )
            if any(price < 0 for price in self.zone_prices.values()):
                raise ValueError("票價不得為負")
        return self

class EventResponse(BaseModel):
    """Event representation sent back to client."""

    model_config = ConfigDict(from_attributes=True)

    id: int
    name: str
    venue: str
    venue_id: int | None = None
    """非 None = 座位場次。客戶端靠它決定要不要先呼叫 /zones 選區。"""

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