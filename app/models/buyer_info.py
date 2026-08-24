from datetime import datetime
from sqlalchemy import DateTime, ForeignKey , String, LargeBinary
from sqlalchemy.orm import Mapped, mapped_column
from sqlalchemy.sql import func
from app.db.base import Base

class BuyerInfo(Base):
    __tablename__ = "buyer_info"
    id: Mapped[int] = mapped_column(primary_key=True)
    # CASCADE:這一列**就是**個資。使用者真的被刪掉時它沒有任何留下來的理由,
    # 而且刪掉它等於 crypto-shred —— ciphertext 與被 KEK 包住的 DEK 一起消失,
    # KEK 還在也解不開(見 app/services/pii.py 的信封加密)。
    user_id: Mapped[int] = mapped_column(
        ForeignKey("users.id", ondelete="CASCADE", name="fk_buyer_info_user_id"),
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