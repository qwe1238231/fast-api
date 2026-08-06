from fastapi import APIRouter, HTTPException, Request, status
from sqlalchemy.exc import IntegrityError

from app.api.deps import DbSession, CurrentUser, limiter
from app.core.config import get_settings
from app.crud.user import create_user
from app.models import User
from app.schemas.user import UserCreate, UserResponse

router = APIRouter(prefix="/users", tags=["users"])

@router.post(
    "/",
    response_model=UserResponse,
    status_code=status.HTTP_201_CREATED,
)
@limiter.limit(get_settings().REGISTER_RATE_LIMIT)
async def register_user(
    request: Request,
    user_in: UserCreate,
    db: DbSession,
) -> User:
    """註冊。**限流是必要的,不是保險**:這是無認證端點,而且每次呼叫都寫一列 DB
    並跑一次 ~220ms 的 Argon2 —— 不擋的話任何人都能免費徵用你的 CPU 與連線池,
    而搶票網站在開賣前本來就會被大量註冊機器人打。雜湊本身已經移出 event loop
    (見 `hash_password_async`),但那只解決「不阻塞」,不解決「被無限徵用」。

    `request` 參數是 slowapi 要求的 —— 它從簽名裡找這個名字來取限流的 key。
    """
    try:
        return await create_user(db, user_in)
    except IntegrityError:
        await db.rollback()
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="Username already registered",
        )
    


@router.get("/me", response_model=UserResponse)
async def get_me(current_user: CurrentUser) -> User:
    return current_user