from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException, status ,Request, Response
from fastapi.security import OAuth2PasswordRequestForm

from app.api.deps import DbSession, limiter, CurrentUser, Redis
from app.core.config import get_settings

from app.core.security import (
    verify_password,
    create_access_token,
    constant_time_dummy_verify,
)
from app.crud.user import get_user_by_username
from app.schemas.token import Token
from app.services.audit import emit_event
import secrets
from datetime import timedelta, datetime, timezone
from app.crud.refresh_token import (
    get_refresh_token_for_update,
    mark_token_used,
    revoke_family,
    issue_refresh_token,
    revoke_all_for_user,
)


router = APIRouter(prefix="/auth", tags=["auth"])

def set_auth_cookies(
        response: Response,
        *,
        refresh_token: str,
        csrf_token: str,
        lifetime_seconds: int,
        secure: bool,
) -> None:
    response.set_cookie(
        key="refresh_token",
        value=refresh_token,
        max_age=lifetime_seconds,
        path="/v1/auth",
        httponly=True,
        secure=secure,
        samesite="lax",
    )
    response.set_cookie(
        key="csrf_token",
        value=csrf_token,
        max_age=lifetime_seconds,
        path="/",
        httponly=False,
        secure=secure,
        samesite="lax",
    )

def clear_auth_cookies(
        response: Response,
        *,
        secure: bool,
) -> None:
    response.delete_cookie(
        key="refresh_token",
        path="/v1/auth",
        secure=secure,
        samesite="lax",
    )
    response.delete_cookie(
        key="csrf_token",
        path="/",
        secure=secure,
        samesite="lax",
    )


@router.post("/token",response_model=Token)
@limiter.limit(get_settings().LOGIN_RATE_LIMIT)
async def login_for_access_token(
    request: Request,
    response: Response,
    form_data: Annotated[OAuth2PasswordRequestForm, Depends()],
    db: DbSession,
    redis: Redis,
) -> Token:
    user = await get_user_by_username(db, username=form_data.username)
    if user is None:
        constant_time_dummy_verify(form_data.password)
        password_valid = False
    else:
        password_valid = verify_password(
            form_data.password, user.hashed_password
        )
    if user is None or not password_valid or not user.is_active:
        await emit_event(
            redis,
            event_type="auth.login_failure",
            actor_ip=request.client.host if request.client else None,
            payload={
        "username_attempted": form_data.username[:64],
        "reason": "invalid_credentials_or_inactive",
        },
            success=False,
        )
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Incorrect username or password",
            headers={"WWW-Authenticate": "Bearer"},
        )
    settings = get_settings()
    sliding = timedelta(days=settings.REFRESH_TOKEN_SLIDING_DAYS)
    absolute = timedelta(days=settings.REFRESH_TOKEN_ABSOLUTE_EXPIRE_DAYS)

    user_agent = request.headers.get("user-agent")
    ip_address = request.client.host if request.client else None

    refresh_plain, _ = await issue_refresh_token(
        db,
        user_id=user.id,
        sliding_lifetime=sliding,
        absolute_expire_lifetime=absolute,
        parent=None,
        user_agent=user_agent,
        ip_address=ip_address,
    )

    csrf_token = secrets.token_urlsafe(32)
    
    await db.commit()

    set_auth_cookies(
        response,
        refresh_token=refresh_plain,
        csrf_token=csrf_token,
        lifetime_seconds=int(sliding.total_seconds()),
        secure=not settings.DEBUG,
    )
    await emit_event(
        redis,
        event_type="auth.login_success",
        actor_user_id=user.id,
        actor_ip=request.client.host if request.client else None,
        payload={
            "user_agent": request.headers.get("user-agent", "")[:256],
        },
    )
    return Token(
        access_token=create_access_token(subject=user.username),
        expires_in=settings.access_token_lifetime_seconds,
    )

@router.post("/refresh",response_model=Token)
@limiter.limit("30/minute")
async def refresh_access_token(
    request: Request,
    response: Response,
    db: DbSession,
) -> Token:
    refresh_plain = request.cookies.get("refresh_token")
    csrf_cookie = request.cookies.get("csrf_token")
    csrf_header = request.headers.get("x-csrf-token")

    if (
        not refresh_plain
        or not csrf_cookie
        or not csrf_header
        or csrf_cookie != csrf_header
    ):
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Invalid credentials",
        )
    token = await get_refresh_token_for_update(db, refresh_plain)
    if token is None:
        raise HTTPException(401, "Invalid credentials")
    if token.revoked_at is not None:
        raise HTTPException(401, "Invalid credentials")
    
    settings = get_settings()
    now = datetime.now(timezone.utc)
    if token.expires_at < now or token.absolute_expires_at < now:
        raise HTTPException(401, "Invalid credentials")
    
    grace = timedelta(seconds=settings.REFRESH_TOKEN_REUSE_GRACE_SECONDS)
    if token.used_at is not None:
        if now - token.used_at > grace:
            await revoke_family(db, token.family_id)
            await db.commit()
            raise HTTPException(401, "Invalid credentials")
    else:
        await mark_token_used(db, token)
    
    sliding = timedelta(days=settings.REFRESH_TOKEN_SLIDING_DAYS)
    absolute = timedelta(days=settings.REFRESH_TOKEN_ABSOLUTE_EXPIRE_DAYS)
    user_agent = request.headers.get("user-agent")
    ip_address = request.client.host if request.client else None

    new_refresh_plain, _ = await issue_refresh_token(
        db,
        user_id=token.user_id,
        sliding_lifetime=sliding,
        absolute_expire_lifetime=absolute,
        parent=token,
        user_agent=user_agent,
        ip_address=ip_address,
    )
    new_csrf = secrets.token_urlsafe(32)
    await db.commit()

    set_auth_cookies(
        response,
        refresh_token=new_refresh_plain,
        csrf_token=new_csrf,
        lifetime_seconds=int(sliding.total_seconds()),
        secure=not settings.DEBUG,
    )
    return Token(
        access_token=create_access_token(subject=str(token.user_id)),
        expires_in=settings.access_token_lifetime_seconds,
    )

@router.post("/logout", status_code=status.HTTP_204_NO_CONTENT)
async def logout(
    request: Request,
    response: Response,
    db: DbSession,
) -> None:
    refresh_plain = request.cookies.get("refresh_token")
    csrf_cookie = request.cookies.get("csrf_token")
    csrf_header = request.headers.get("x-csrf-token")

    if (
        not refresh_plain
        or not csrf_cookie
        or not csrf_header
        or csrf_cookie != csrf_header
    ):
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Invalid credentials",
        )
    token = await get_refresh_token_for_update(db, refresh_plain)
    if token is not None:
        await revoke_family(db, token.family_id)
        await db.commit()

    settings = get_settings()
    clear_auth_cookies(response, secure=not settings.DEBUG)

@router.post("/logout-all", status_code=status.HTTP_204_NO_CONTENT)
async def logout_all(
    response: Response,
    db: DbSession,
    current_user: CurrentUser,
) -> None:
    await revoke_all_for_user(db, current_user.id)
    await db.commit()

    settings = get_settings()
    clear_auth_cookies(response, secure=not settings.DEBUG)

