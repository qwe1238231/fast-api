"""Buyer info service — orchestrates PII encryption + DB insert.

Service layer is the only place that handles plaintext PII (briefly).
Caller (route) gives plaintext, gets back encrypted result.
"""
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession
from app.core.exceptions import BuyerInfoAlreadyExists, NationalIdAlreadyRegistered
from app.crud.buyer_info import create_buyer_info, get_buyer_info_by_user_id
from app.models.buyer_info import BuyerInfo
from app.services.pii import encrypt_pii, lookup_hash


async def register_buyer_info(
        db: AsyncSession,
        *,
        user_id: int,
        real_name: str,
        national_id: str,
) -> BuyerInfo:
    """Create buyer info for a user. Encrypts PII before storing.
    
    Raises BuyerInfoAlreadyExists if user already has one.
    Raises NationalIdAlreadyRegistered if national_id is taken by another user."""
    
    existing = await get_buyer_info_by_user_id(db, user_id)
    if existing is not None:
        raise BuyerInfoAlreadyExists(user_id=user_id)
    
    ciphertext, dek_encrypted = encrypt_pii(national_id)
    lookup = lookup_hash(national_id)

    try:
        info = await create_buyer_info(
            db,
            user_id=user_id,
            real_name=real_name,
            national_id_ciphertext=ciphertext,
            national_id_dek_encrypted=dek_encrypted,
            national_id_lookup_hash=lookup,
        )
    except IntegrityError:
        await db.rollback()
        raise NationalIdAlreadyRegistered()
    
    return info 