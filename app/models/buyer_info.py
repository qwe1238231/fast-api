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

    national_id_lookup_hash: Mapped[str] = mapped_column(LargeBinary, nullable=False, unique=True, index=True,)

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