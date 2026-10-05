from fastapi import APIRouter, HTTPException, Request, status
from sqlalchemy.exc import IntegrityError

from app.api.deps import (
    CurrentAdmin,
    CurrentUser,
    DbSession,
    Redis,
    client_ip,
    enforce_ip_rate_limit,
)
from app.core.config import get_settings
from app.crud.user import create_user, get_user_by_id
from app.models import User
from app.schemas.user import UserCreate, UserResponse
from app.services.audit import emit_event as emit_audit_event
from app.services.erasure import erase_user, is_erased

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


@router.delete("/{user_id}", response_model=UserResponse)
async def erase_user_endpoint(
    user_id: int,
    request: Request,
    db: DbSession,
    redis: Redis,
    current_admin: CurrentAdmin,
) -> User:
    """個資抹除請求。**匿名化,不是刪除** —— 見 app/services/erasure.py。

    走管理員而不是 `/me`,兩個理由:個資法的刪除請求是向資料控管者提出的,而且
    這個動作要留下一筆說得出「誰執行的」的稽核紀錄。使用者自助抹除自己的話,
    actor 就是那個剛剛被匿名化的人。

    回 200 帶匿名化後的 user 而不是 204:呼叫端需要看到結果長什麼樣才能確認
    抹除真的生效了。重複請求是 no-op 也回 200 —— 抹除是冪等的,第二次收到 404
    或 409 只會讓重試邏輯變複雜,而抹除請求本來就可能被重送。
    """
    user = await get_user_by_id(db, user_id)
    if user is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND, detail="User not found"
        )
    if is_erased(user):
        return user

    await erase_user(db, user=user)
    await db.commit()
    # 稽核在 commit 之後,理由跟事件快取失效同一條:先送出、後 rollback 的話,
    # 紀錄裡會有一次從未發生的抹除。actor_user_id 記的是執行的管理員,
    # target 才是被抹除的人 —— 而這筆紀錄會活得比那個帳號久(SET NULL 只在
    # 管理員自己被刪掉時生效,被抹除的人是 target,不受影響)。
    await emit_audit_event(
        redis,
        event_type="user.erased",
        actor_user_id=current_admin.id,
        actor_ip=client_ip(request),
        target_type="user",
        target_id=str(user_id),
    )
    return user