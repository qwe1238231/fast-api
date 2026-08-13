from fastapi import APIRouter, HTTPException, Request, status
from sqlalchemy.exc import IntegrityError

from app.api.deps import DbSession, CurrentUser, Redis, enforce_ip_rate_limit
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
async def register_user(
    request: Request,
    user_in: UserCreate,
    db: DbSession,
    redis: Redis,
) -> User:
    """註冊。**限流是必要的,不是保險**:這是無認證端點,而且每次呼叫都寫一列 DB
    並跑一次 ~220ms 的 Argon2 —— 不擋的話任何人都能免費徵用你的 CPU 與連線池,
    而搶票網站在開賣前本來就會被大量註冊機器人打。雜湊本身已經移出 event loop
    (見 `hash_password_async`),但那只解決「不阻塞」,不解決「被無限徵用」。
    """
    await enforce_ip_rate_limit(
        request, redis, bucket="register", rate=get_settings().REGISTER_RATE_LIMIT
    )
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