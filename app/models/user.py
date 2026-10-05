from datetime import datetime

from sqlalchemy import DateTime, String
from sqlalchemy.orm import Mapped, mapped_column
from sqlalchemy.sql import expression, func
from app.db.base import Base


class User(Base):
    __tablename__ = "users"
    id: Mapped[int] = mapped_column(primary_key=True)
    username: Mapped[str] = mapped_column(
        String(64),
        unique=True,
        index=True,
        nullable=False,
    )
    hashed_password: Mapped[str] = mapped_column(
        String(255),
        nullable=False,
    )
    is_active: Mapped[bool] = mapped_column(
        default=True,
        server_default=expression.true(),
        nullable=False,
    )
    is_admin: Mapped[bool] = mapped_column(
        default=False,
        server_default=expression.false(),
        nullable=False,
    )
    # 回填說明:migration 之前的帳號拿到的是 migration 當下的時間 —— 真實建立
    # 時間已失傳,這是誠實的近似(NULL 會讓每個讀取端都要處理特例,更糟)。
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        server_default=func.now(),
        nullable=False,
    )

    def __repr__(self) -> str:
        return f"<User(id={self.id}, username={self.username})>"