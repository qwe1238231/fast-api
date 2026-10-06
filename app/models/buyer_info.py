from datetime import datetime
from sqlalchemy import DateTime, ForeignKey , String, LargeBinary
from sqlalchemy.orm import Mapped, mapped_column
from sqlalchemy.sql import func
from app.db.base import Base

class BuyerInfo(Base):
    __tablename__ = "buyer_info"
    # user_id 直接當主鍵:這張表是嚴格 1:1(一個人一份實名),surrogate id 只是
    # 多一個序列跟多一個索引,而全 repo 沒有任何地方引用過它。
    # CASCADE:這一列**就是**個資。使用者真的被刪掉時它沒有任何留下來的理由,
    # 而且刪掉它等於 crypto-shred —— ciphertext 與被 KEK 包住的 DEK 一起消失,
    # KEK 還在也解不開(見 app/services/pii.py 的信封加密)。
    user_id: Mapped[int] = mapped_column(
        ForeignKey("users.id", ondelete="CASCADE", name="fk_buyer_info_user_id"),
        primary_key=True,
    )
    
    real_name: Mapped[str] = mapped_column(String(64), nullable=False,)

    national_id_ciphertext: Mapped[bytes] = mapped_column(LargeBinary, nullable=False,)

    national_id_dek_encrypted: Mapped[bytes] = mapped_column(LargeBinary, nullable=False,)

    # BYTEA,不是 str —— pii.lookup_hash() 回的是 raw digest。標註寫 str 的話
    # type checker 對這個欄位就失效了:拿 str 去寫 bytea 不會靜默失敗,是 asyncpg
    # 直接丟型別錯誤,但那要跑到才知道。
    #
    # unique 的角色是**守門,不是查詢加速**:全 repo 沒有任何 SELECT 按這一欄過濾
    # (services/buyer_info.py 只寫入),它存在的唯一理由是「一張身分證只能綁一個
    # 帳號」—— 第二個人拿同一張證來註冊,INSERT 撞唯一索引,IntegrityError 被 service
    # 翻成 NationalIdAlreadyRegistered。檢查放在 DB 而不是應用層,因為「先 SELECT 再
    # INSERT」擋不住兩個請求同時通過 SELECT 的那條縫;唯一索引是唯一不會漏的地方。
    # unique=True + index=True 在 SQLAlchemy 只建一個 UNIQUE INDEX,不是兩個。
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