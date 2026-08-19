from datetime import datetime
from enum import Enum 

from sqlalchemy import CheckConstraint, DateTime, ForeignKey, String, text
from sqlalchemy.orm import Mapped, mapped_column
from sqlalchemy.sql import func
from sqlalchemy import Enum as SAEnum
from app.db.base import Base

class EventStatus(str, Enum):
    DRAFT = "draft"
    PUBLISHED = "published"
    CANCELLED = "cancelled"
    
class Event(Base):
    __tablename__ = "events"
    __table_args__ = (
        # total_seats is the ceiling the whole oversell / inventory-reconcile logic
        # trusts; a 0/negative from an admin typo would poison it. Guard at the DB.
        CheckConstraint("total_seats > 0", name="ck_events_total_seats_pos"),
        CheckConstraint("price_cents >= 0", name="ck_events_price_nonneg"),
    )
    id: Mapped[int] = mapped_column(primary_key=True)
    name: Mapped[str] = mapped_column(String(255), nullable=False)
    venue: Mapped[str] = mapped_column(String(255), nullable=False)
    # 座位圖要掛在正規化的場館上,自由字串沒地方掛 block/seat。刻意先做成
    # nullable 的加法:沒有座位圖的舊場次(以及只用 total_seats 計數的路徑)
    # 照常運作,座位功能只對 venue_id 有值的場次生效。等座位流程完整上線、
    # 舊資料回填完成,才把 venue 字串下架。
    venue_id: Mapped[int | None] = mapped_column(
        ForeignKey("venues.id"), nullable=True, index=True
    )
    starts_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    ends_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    sale_starts_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    sale_ends_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    # Waiting-room window. Nullable → the service layer falls back to
    # sale_starts_at minus configured lead/buffer defaults (behaviour "A").
    queue_opens_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    queue_closes_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    total_seats: Mapped[int] = mapped_column(nullable=False)
    price_cents: Mapped[int] = mapped_column(nullable=False)
    status: Mapped[EventStatus] = mapped_column(
        SAEnum(
            EventStatus,
            native_enum=False,
            values_callable=lambda x : [ e.value for e in x ],
            ),
        nullable=False,
        default=EventStatus.DRAFT,
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        nullable=False,
        server_default=func.now(),
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        nullable=False,
        server_default=func.now(),
        onupdate=func.now(),
    )
    # 樂觀鎖版本號。防的不是併發搶單,而是後台的 lost update:兩個管理員同時
    # 開編輯頁,後存檔的那份帶著過期的欄位值,把先存檔的改動靜默蓋回去。
    # 值由 SQLAlchemy 自己管(INSERT 填 1、每次 flush +1),所以不寫 default;
    # server_default 是給 migration 回填既有列用的(NOT NULL 加欄位需要它)。
    version: Mapped[int] = mapped_column(nullable=False, server_default=text("1"))

    # 掛上之後,任何經 ORM flush 的 Event 修改都會自動帶 `WHERE version = :old`,
    # 不符即丟 StaleDataError。注意這只對 ORM flush 生效 —— 改用
    # `update(Event).where(...)` 這種 Core statement 會繞過版本檢查而靜默失效。
    __mapper_args__ = {"version_id_col": version}