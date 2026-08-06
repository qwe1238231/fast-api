from datetime import datetime
from enum import StrEnum
from typing import Annotated
from uuid import UUID

from pydantic import BaseModel, Field, ConfigDict
from app.core.config import max_purchasable
from app.models.order import OrderStatus


class OrderCreate(BaseModel):
    """Request body for POST /v1/orders."""

    event_id: int
    quantity: Annotated[int, Field(ge=1, le=max_purchasable())]
    """張數上限取 min(單筆上限, 每人限購)。

    寫死 `le=10` 而限購是 4 的話,5~10 張會通過驗證、跑完整條入場券與 Redis 路徑,
    最後才拿到 409 —— 而 OpenAPI 仍然對外宣告可以買 10 張,前端的數量選單就照著
    生出四個永遠買不到的選項。上限在請求邊界就講清楚,那些請求連進來都不會。

    在 class 定義時求值(不是每次請求),所以改設定要重啟 —— 跟 Settings 的
    lru_cache 一致,而且換來的是 OpenAPI 文件裡有正確的上限。
    """
    zone_id: int | None = None
    """要買哪一區。有座位圖的場次必填(票價與配位都以 zone 為單位);
    無座位圖的舊場次必須留空。"""


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
    zone_id: int | None = None
    """買的是哪一區。無座位圖的場次為 None。"""

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