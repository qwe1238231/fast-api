from datetime import datetime
from sqlalchemy import DateTime, ForeignKey , String, LargeBinary
from sqlalchemy.orm import Mapped, mapped_column
from sqlalchemy.sql import func
from app.db.base import Base

class BuyerInfo(Base):
    __tablename__ = "buyer_info"
    id: Mapped[int] = mapped_column(primary_key=True)
    user_id: Mapped[int] = mapped_column(
        ForeignKey("users.id"),
        nullable=False,
        unique=True,
    )
    
    real_name: Mapped[str] = mapped_column(String(64), nullable=False,)

    national_id_ciphertext: Mapped[bytes] = mapped_column(LargeBinary, nullable=False,)

    national_id_dek_encrypted: Mapped[bytes] = mapped_column(LargeBinary, nullable=False,)

    # BYTEA,不是 str —— pii.lookup_hash() 回的是 raw digest。標註寫 str 的話
    # type checker 對這個欄位就失效了,而它是查詢鍵:拿 str 去比對 bytea 不會
    # 靜默失敗,是 asyncpg 直接丟型別錯誤,但那要跑到才知道。
    national_id_lookup_hash: Mapped[bytes] = mapped_column(LargeBinary, nullable=False, unique=True, index=True,)

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