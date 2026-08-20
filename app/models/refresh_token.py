from datetime import datetime
from uuid import UUID , uuid4
from sqlalchemy import BigInteger, Uuid, DateTime, ForeignKey , String
from sqlalchemy.orm import Mapped, mapped_column
from sqlalchemy.sql import func
from app.db.base import Base


class RefreshToken(Base):
    __tablename__ = "refresh_tokens"
    # BIGINT,而且**這張表是四張裡燒得最快的**。序列消耗跟登入次數不成比例:
    # 每次登入開一條,之後每一次 rotation 再開一條,而清理 job 刪掉的列不會把
    # 序列值還回來。100 萬次登入/天 × 10 次輪替 ≈ 1000 萬/天,int4 撐約 210 天。
    id: Mapped[int] = mapped_column(BigInteger, primary_key=True)

    family_id: Mapped[UUID] = mapped_column(
        Uuid(as_uuid=True),
        index=True,
        nullable=False,
        default=uuid4,
    )

    expires_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        nullable=False,
    )

    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        nullable=False,
        server_default=func.now(),
    )

    # 跟著 id 一起變 BIGINT —— 自我外鍵的兩端型別必須一致,不然 RI 檢查每次都要
    # 隱式轉型,索引也用不上。
    parent_id: Mapped[int | None] = mapped_column(
        BigInteger,
        ForeignKey("refresh_tokens.id"),
        nullable=True,
    )

    # CASCADE:session 離開了它的主人就沒有意義,留著只是一堆指不到人的雜湊。
    user_id: Mapped[int] = mapped_column(
        ForeignKey("users.id", ondelete="CASCADE", name="fk_refresh_tokens_user_id"),
        nullable=False,
        index=True,
    )

    token_hash: Mapped[str] = mapped_column(
        String(64),
        unique=True,
        index=True,
        nullable=False,
    )

    used_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True),
        nullable=True,
    )

    revoked_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True),
        nullable=True,
    )

    user_agent: Mapped[str | None] = mapped_column(
        String(512),
        nullable=True,
    )   

    ip_address: Mapped[str | None] = mapped_column(
        String(45),
        nullable=True,
    )

    absolute_expires_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        nullable=False,
    )