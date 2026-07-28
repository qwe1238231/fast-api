from datetime import datetime
from enum import Enum 

from sqlalchemy import CheckConstraint, DateTime, String
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