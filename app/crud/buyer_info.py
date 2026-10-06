from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.buyer_info import BuyerInfo


async def create_buyer_info(
        db: AsyncSession,
        *,
        user_id: int,
        real_name: str,
        national_id_ciphertext: bytes,
        national_id_dek_encrypted: bytes,
        national_id_lookup_hash: bytes,
        kek_version: int,
) -> BuyerInfo:
    """Insert a new buyer_info row. Caller must commit.

    Caller passes pre-encrypted PII (this layer doesn't know about crypto), including
    which KEK version wrapped the DEK — the row must remember that to be decryptable."""
    info = BuyerInfo(
        user_id=user_id,
        real_name=real_name,
        national_id_ciphertext=national_id_ciphertext,
        national_id_dek_encrypted=national_id_dek_encrypted,
        national_id_lookup_hash=national_id_lookup_hash,
        kek_version=kek_version,
    )
    db.add(info)
    await db.flush()
    return info


async def get_buyer_info_by_user_id(
        db: AsyncSession,
        user_id: int,
) -> BuyerInfo | None:
    """Find a user's buyer info, if any."""
    stmt = select(BuyerInfo).where(BuyerInfo.user_id == user_id)
    result = await db.execute(stmt)
    return result.scalar_one_or_none()