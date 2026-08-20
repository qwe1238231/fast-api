from datetime import datetime
from typing import Any
from sqlalchemy import BigInteger, DateTime, ForeignKey, String
from sqlalchemy.dialects.postgresql import JSONB 
from sqlalchemy.orm import Mapped, mapped_column
from sqlalchemy.sql import func 

from app.db.base import Base


class AuditLog(Base):
    __tablename__ = "audit_logs"

    # BIGINT。每一次管理動作與每一次認證事件都是一列,而這張表沒有保留策略,
    # 是全庫唯一真正無界成長的那張(見待辦的 audit_logs 分區)。
    id: Mapped[int] = mapped_column(BigInteger, primary_key=True)

    event_type: Mapped[str] = mapped_column(
        String(64),
        nullable=False,
        index=True,
    )
    
    # SET NULL:稽核紀錄必須比它記錄的那個人活得久 —— 「誰在什麼時候改了什麼」
    # 的價值恰恰在事後,而 CASCADE 會讓「刪掉帳號」變成「湮滅自己的紀錄」。
    # 這一欄本來就 nullable(系統自身的動作沒有 actor),所以 SET NULL 不需要
    # 任何 schema 讓步:actor 消失了,事件本身還在。
    actor_user_id: Mapped[int | None] = mapped_column(
        ForeignKey("users.id", ondelete="SET NULL", name="fk_audit_logs_actor_user_id"),
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