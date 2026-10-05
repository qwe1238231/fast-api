from datetime import datetime
from uuid import UUID , uuid4
from sqlalchemy import BigInteger, Index, Uuid, DateTime, ForeignKey , String, text
from sqlalchemy.orm import Mapped, mapped_column
from sqlalchemy.sql import func
from app.db.base import Base


class RefreshToken(Base):
    __tablename__ = "refresh_tokens"
    __table_args__ = (
        # purge_expired 每晚跑,而它的 WHERE 是這兩欄的 OR。少了索引就是整張表的
        # seq scan —— 實測 50 萬列時讀 9615 個 block、花 77ms **刪掉 0 列**,而且
        # 成本隨表線性成長。加上之後 Postgres 用 BitmapOr 把兩個索引合起來,
        # 同一句變成讀 4 個 block、0.13ms。
        #
        # (OR 本身不是問題 —— 這是常見的誤解。BitmapOr 處理得很好,前提是兩邊
        #  各自有索引可用。)
        Index("ix_refresh_tokens_absolute_expires_at", "absolute_expires_at"),
        # revoked_at 用 partial:絕大多數 token 從來沒有被撤銷過,NULL 的那些列
        # 對這個查詢毫無意義。實測索引從 3.4 MB 掉到 8 KB。
        Index(
            "ix_refresh_tokens_revoked_at", "revoked_at",
            postgresql_where=text("revoked_at IS NOT NULL"),
        ),
        # parent_id 是純寫入欄位(rotation 設、沒人讀),但它是自我外鍵 —— 每刪一列
        # parent,RI trigger 就要查一次「有沒有人指著我」。沒有索引時那是**每列一次
        # seq scan**:實測 50 萬 token 刪 2000 對 parent+child,trigger 佔 57.4 秒;
        # 加了索引 13.7 ms(4200 倍)。purge_expired 每晚在做的正是這種成批刪除。
        #
        # partial(略過 NULL,也就是每個 family 的頭)可用:planner 能從 RI 查詢的
        # `parent_id = $1` 推出 IS NOT NULL —— 上面那組實測就是用這個 partial 量的。
        Index(
            "ix_refresh_tokens_parent_id", "parent_id",
            postgresql_where=text("parent_id IS NOT NULL"),
        ),
    )
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