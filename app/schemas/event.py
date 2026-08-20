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

    @model_validator(mode="after")
    def _check_window(self) -> "EventCreate":
        """時間欄位的先後。DB 有對應的 CHECK(ck_events_show_window /
        ck_events_sale_window),這裡是同一組規則的前置版本。

        兩層都要有,但理由不是「保險」而是**錯誤碼**:少了這一層,倒過來的日期會
        一路走到 INSERT 才被 Postgres 擋下,而專案沒有全域的 IntegrityError
        handler —— 管理員收到的會是 500,不是「ends_at 必須晚於 starts_at」。
        DB 那層擋的是繞過這個 schema 的寫入路徑(migration、修資料的手動 SQL)。
        """
        if self.starts_at >= self.ends_at:
            raise ValueError("starts_at 必須早於 ends_at")
        if self.sale_starts_at >= self.sale_ends_at:
            raise ValueError("sale_starts_at 必須早於 sale_ends_at")
        return self

class EventUpdate(BaseModel):
    """Request body for PATCH /v1/events/{id}(後台編輯)。

    **version 是必填的**,而且必須是客戶端 GET 到的那個值。少了它,伺服器比對的
    就只是自己幾毫秒前才讀出來的版本 —— 永遠吻合、永遠放行,樂觀鎖等於沒裝。
    危險的間隔是使用者盯著編輯頁的那幾分鐘,它橫跨兩個請求。

    `extra="forbid"`:PATCH 的欄位是「有給才改」,打錯欄位名在寬鬆模式下會被靜默
    忽略 —— 管理員看到 200 卻什麼都沒變,是最難查的一種 bug。

    刻意**不可**編輯的欄位,各有各的理由:
      total_seats  庫存上限,Redis 那份已經照它初始化了;改它要連同庫存一起搬,
                   不是一次 UPDATE 的事。
      venue_id     等於抽換座位圖,而底下可能已經有 hold 與已售座位。
      status       有自己的狀態機與端點(POST /{id}/publish)。
      zone_prices  在另一張表(EventZonePrice),有自己的版本;混進來會讓一次
                   PATCH 橫跨兩個樂觀鎖的粒度。
    """

    model_config = ConfigDict(extra="forbid")

    version: int = Field(ge=1, description="GET 回來的 version,原樣送回")

    name: str | None = Field(default=None, min_length=1, max_length=255)
    venue: str | None = Field(default=None, min_length=1, max_length=255)
    starts_at: datetime | None = None
    ends_at: datetime | None = None
    sale_starts_at: datetime | None = None
    sale_ends_at: datetime | None = None
    price_cents: int | None = Field(default=None, ge=0)

    # 這兩個在 DB 是 nullable,所以顯式傳 null 的意思是「清掉,回去用預設推導」。
    # 服務層用 exclude_unset 而不是 exclude_none 取變更集,才分得出「沒給」跟
    # 「給了 null」—— 用 exclude_none 的話這兩個欄位永遠清不掉。
    queue_opens_at: datetime | None = None
    queue_closes_at: datetime | None = None


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

    version: int
    """樂觀鎖版本。後台編輯時要原樣放回 PATCH 的 body —— 不吐出來,前端就無從帶回,
    整條鎖也就形同虛設。"""


class QueueStatusResponse(BaseModel):
    """Waiting-room state for the current user."""

    admitted: bool
    sold_out: bool = False                 # inventory exhausted — stop waiting, nothing to buy
    paused: bool = False                   # admission frozen by the circuit breaker (downstream unhealthy)
    people_ahead: int | None = None        # not-yet-admitted users ahead (0 = next); None if admitted/unregistered
    poll_after_seconds: int | None = None  # suggested backoff before polling again; None once admitted/sold out
    access_token: str | None = None        # single-use admission pass for POST /orders/; set only when admitted