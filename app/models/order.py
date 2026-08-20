from datetime import datetime
from enum import Enum
from uuid import UUID
from sqlalchemy import Index, text
from sqlalchemy import DateTime, ForeignKey, ForeignKeyConstraint, String, Uuid
from sqlalchemy.orm import Mapped, mapped_column
from sqlalchemy.sql import func
from sqlalchemy import Enum as SAEnum
from sqlalchemy import Index, text, CheckConstraint
from app.db.base import Base



class OrderStatus(str, Enum):
    PENDING = "pending"
    PAID = "paid"
    CONFIRMED = "confirmed"
    EXPIRED = "expired"
    CANCELLED = "cancelled"



class Order(Base):
    __tablename__ = "orders"
    __table_args__ = (
        Index(
            "ix_orders_active", "event_id", "status",
            postgresql_where=text("status IN ('pending', 'paid', 'confirmed')"),
        ),
        # Keyset pagination for list_orders_for_user: equality on user_id, then
        # the (created_at, id) sort/cursor columns -> single index range scan.
        Index("ix_orders_user_created", "user_id", "created_at", "id"),
        # Expiry sweep (worker.expire_pending_orders) filters exactly these rows every
        # minute; a partial index on created_at keeps that a tiny range scan instead of
        # a seq scan of orders, and stays small (rows drop out as they leave PENDING).
        Index(
            "ix_orders_pending_sweep", "created_at",
            postgresql_where=text("status = 'pending' AND payment_provider_id IS NULL"),
        ),
        CheckConstraint(
            "status IN ('pending', 'paid', 'confirmed', 'expired', 'cancelled')",
            name="ck_orders_status",
        ),
        # Values must stay sane even when a row is INSERTed by the order-consumer
        # worker off a Redis Stream payload (which bypasses the Pydantic request layer).
        CheckConstraint("quantity > 0", name="ck_orders_quantity_pos"),
        CheckConstraint("total_price_cents >= 0", name="ck_orders_total_nonneg"),
        # 每個 milestone status 必須有對應的時間戳(forward correspondence)。
        CheckConstraint(
            "status <> 'paid' OR paid_at IS NOT NULL",
            name="ck_orders_paid_at",
        ),
        CheckConstraint(
            "status <> 'confirmed' OR confirmed_at IS NOT NULL",
            name="ck_orders_confirmed_at",
        ),
        CheckConstraint(
            "status <> 'expired' OR expired_at IS NOT NULL",
            name="ck_orders_expired_at",
        ),
        CheckConstraint(
            "status <> 'cancelled' OR cancelled_at IS NOT NULL",
            name="ck_orders_cancelled_at",
        ),
        # (event_id, zone_id) 指向 event_zone_prices 的**主鍵**。取代原本的單欄
        # fk_orders_zone_id —— 那條只保證「zone 存在」,擋不住「這一區根本不屬於
        # 這場次的場館」。zone 的存在性由 event_zone_prices.zone_id 遞移保證,
        # 所以單欄那條是冗餘的,留著只是在最熱的寫入路徑上多一次查表。
        #
        # 附帶保證:有訂單的 zone 一定有票價。少了價格列的 zone 算進 total_seats
        # 卻永遠賣不掉(見 publish_event 的 ZonePricesIncomplete),現在連「訂單指
        # 到沒定價的區」這條路也被 DB 封死。
        #
        # NULL 語意靠 Postgres 的預設 MATCH SIMPLE:任一參照欄位是 NULL 就整條放行。
        # 所以無座位圖的場次(zone_id IS NULL)完全不受影響。**不要改成 MATCH FULL**
        # —— event_id 是 NOT NULL,那會讓 zone_id 變成實質必填,把非座位場次全擋死。
        ForeignKeyConstraint(
            ["event_id", "zone_id"],
            ["event_zone_prices.event_id", "event_zone_prices.zone_id"],
            name="fk_orders_event_zone_price",
        ),
    )
    id: Mapped[int] = mapped_column(primary_key=True)
    # RESTRICT 不是預設值的同義詞,是一句宣告:**訂單不隨使用者消失**。訂單是會計
    # 憑證(商業會計法要求保存五年),個資法的刪除請求不凌駕法定保存義務,所以
    # 「抹除使用者」的正解是匿名化而不是連坐刪除 —— 見 app/services/erasure.py。
    # 這條 FK 的作用是讓任何人想抄捷徑時,資料庫會先擋下來。
    user_id: Mapped[int] = mapped_column(
        ForeignKey("users.id", ondelete="RESTRICT", name="fk_orders_user_id"),
        nullable=False,
    )  # covered by ix_orders_user_created
    event_id: Mapped[int] = mapped_column(ForeignKey("events.id"), nullable=False)  # covered by ix_orders_active (event_id, status)
    # 買的是哪一區。分區票價下這是必要的來源資訊,但金額本身仍以
    # total_price_cents 的快照為準(所以之後改價動不到已成立的訂單,
    # webhook 的金額驗證也不必重算)。nullable:無座位圖的場次留空。
    # 外鍵是 __table_args__ 裡的複合 fk_orders_event_zone_price,不是單欄的。
    zone_id: Mapped[int | None] = mapped_column(nullable=True)
    quantity: Mapped[int] = mapped_column(nullable=False)
    total_price_cents: Mapped[int] = mapped_column(nullable=False)
    status: Mapped[OrderStatus] = mapped_column(
        SAEnum(
            OrderStatus,
            native_enum=False,
            values_callable=lambda x :[e.value for e in x],
            ),
        nullable=False,
        default=OrderStatus.PENDING,
    )
    idempotency_key: Mapped[UUID] = mapped_column(
        Uuid(as_uuid=True),
        nullable=False,
        unique=True,
        index=True,
    )
    payment_provider_id: Mapped[str | None] = mapped_column(String(255), nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        server_default=func.now(),
        nullable=False,
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        server_default=func.now(),
        onupdate=func.now(),
        nullable=False,
    )
    paid_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    confirmed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    expired_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    cancelled_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)