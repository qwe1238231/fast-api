from datetime import datetime
from typing import Any
from sqlalchemy import DateTime, ForeignKey, String
from sqlalchemy.dialects.postgresql import JSONB 
from sqlalchemy.orm import Mapped, mapped_column
from sqlalchemy.sql import func 

from app.db.base import Base


class AuditLog(Base):
    __tablename__ = "audit_logs"

    id: Mapped[int] = mapped_column(primary_key=True)

    event_type: Mapped[str] = mapped_column(
        String(64),
        nullable=False,
        index=True,
    )
    
    actor_user_id: Mapped[int | None] = mapped_column(
        ForeignKey("users.id"),
        nullable=True,
        index=True,
    )

    actor_ip: Mapped[str | None] = mapped_column(
        String(45),  # Supports IPv4 and IPv6
        nullable=True,
    )

    target_type: Mapped[str | None] = mapped_column(
        String(32),
        nullable=True,
    )

    target_id: Mapped[str | None] = mapped_column(
        nullable=True,
    )

    payload: Mapped[dict[str, Any] | None] = mapped_column(
        JSONB,
        nullable=True,
        default=dict,
    )

    success: Mapped[bool] = mapped_column(
        nullable=False,
        default=True,
    )
    error_code: Mapped[str | None] = mapped_column(
        String(64),
        nullable=True,
    )

    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        server_default=func.now(),
        nullable=False,
        index=True,
    )