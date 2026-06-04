from fastapi import APIRouter, status

from app.api.deps import CurrentUser, DbSession
from app.core.exceptions import BuyerInfoNotFound
from app.crud.buyer_info import get_buyer_info_by_user_id
from app.schemas.buyer_info import BuyerInfoCreate, BuyerInfoResponse
from app.services.buyer_info import register_buyer_info
from app.services.pii import decrypt_pii


router = APIRouter(prefix="/buyer-info", tags=["buyer-info"])


@router.post(
    "/",
    response_model=BuyerInfoResponse,
    status_code=status.HTTP_201_CREATED,
)
async def create_my_buyer_info(
    payload: BuyerInfoCreate,
    current_user: CurrentUser,
    db: DbSession,
) -> BuyerInfoResponse:
    """Register buyer info for current user. Idempotent: 409 if already exists."""
    info = await register_buyer_info(
        db,
        user_id=current_user.id,
        real_name=payload.real_name,
        national_id=payload.national_id,
    )
    await db.commit()

    return BuyerInfoResponse(
        real_name=info.real_name,
        national_id=payload.national_id,
    )

@router.get("/me", response_model=BuyerInfoResponse)
async def get_my_buyer_info(
    current_user: CurrentUser,
    db: DbSession,
) -> BuyerInfoResponse:
    """Get current user's buyer info, with PII decrypted."""
    info = await get_buyer_info_by_user_id(db, current_user.id)
    if info is None:
        raise BuyerInfoNotFound(user_id=current_user.id)
    
    national_id = decrypt_pii(
        info.national_id_ciphertext,
        info.national_id_dek_encrypted,
    )
    
    return BuyerInfoResponse(
        real_name=info.real_name,
        national_id=national_id,
    )