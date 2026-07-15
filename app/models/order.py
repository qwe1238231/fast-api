from datetime import datetime
from enum import Enum
from uuid import UUID
from sqlalchemy import Index, text
from sqlalchemy import DateTime, ForeignKey, String, Uuid
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
        CheckConstraint(
            "status IN ('pending', 'paid', 'confirmed', 'expired', 'cancelled')",
            name="ck_orders_status",
        ),
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
    )
    id: Mapped[int] = mapped_column(primary_key=True)
    user_id: Mapped[int] = mapped_column(ForeignKey("users.id"), nullable=False)  # covered by ix_orders_user_created
    event_id: Mapped[int] = mapped_column(ForeignKey("events.id"), nullable=False, index=True)
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