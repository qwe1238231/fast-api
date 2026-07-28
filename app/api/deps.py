from typing import Annotated

import jwt
from fastapi import Depends, HTTPException, status, Request, Query
from redis.asyncio import Redis as RedisClient
from stripe import StripeClient
from fastapi.security import OAuth2PasswordBearer
from jwt.exceptions import InvalidTokenError
from pydantic import ValidationError
from slowapi import Limiter
from slowapi.util import get_remote_address
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import get_settings
from app.crud.user import get_user_by_username
from app.db.session import get_db
from app.models.user import User
from app.schemas.token import TokenPayload

oauth2_scheme = OAuth2PasswordBearer(tokenUrl="/v1/auth/token")
# EventSource can't send an Authorization header, so the SSE stream also reads the
# token from a query param; auto_error=False lets get_stream_user fall back to it.
oauth2_scheme_optional = OAuth2PasswordBearer(tokenUrl="/v1/auth/token", auto_error=False)
limiter = Limiter(key_func=get_remote_address)

DbSession = Annotated[AsyncSession, Depends(get_db)]

async def get_redis(request: Request) -> RedisClient:
    """Return the app-wide Redis client.
    
    Lifespan stored it on app.state; here we just hand it back so endpoints
    can use `Annotated[Redis, Depends(get_redis)]` for clean DI.
    """
    return request.app.state.redis
Redis = Annotated[RedisClient, Depends(get_redis)]

async def get_stripe(request: Request) -> StripeClient:
    """Return the app-wide async Stripe client (created in lifespan)."""
    return request.app.state.stripe
Stripe = Annotated[StripeClient, Depends(get_stripe)]

async def _load_user_from_token(token: str | None, db: AsyncSession) -> User:
    """Validate a JWT access token and load its user (no active-status check).

    Shared by the header-based get_current_user and the header-or-query
    get_stream_user. Raises 401 on a missing/invalid token or unknown user.
    """
    credentials_exception = HTTPException(
        status_code=status.HTTP_401_UNAUTHORIZED,
        detail="Could not validate credentials",
        headers={"WWW-Authenticate": "Bearer"},
    )
    if not token:
        raise credentials_exception
    settings = get_settings()
    try:
        payload = TokenPayload(**jwt.decode(
            token, settings.SECRET_KEY, algorithms=[settings.ALGORITHM],
        ))
    except (InvalidTokenError, ValidationError):
        raise credentials_exception

    user = await get_user_by_username(db, username=payload.sub)
    if user is None:
        raise credentials_exception
    return user


async def get_current_user(
        token: Annotated[str, Depends(oauth2_scheme)],
        db: DbSession,
) -> User:
    return await _load_user_from_token(token, db)


async def get_current_active_user(
        current_user: Annotated[User, Depends(get_current_user)],
) -> User:
    if not current_user.is_active:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Inactive user",
        )
    return current_user

CurrentUser = Annotated[User, Depends(get_current_active_user)]


async def get_stream_user(
        db: DbSession,
        header_token: Annotated[str | None, Depends(oauth2_scheme_optional)] = None,
        access_token: Annotated[str | None, Query()] = None,
) -> User:
    """Auth for the SSE/EventSource stream: JWT from the Authorization header
    (preferred, for API clients) or an ?access_token= query param (for browsers —
    native EventSource can't set headers). Header wins if both are present.

    SECURITY: a token in the query string lands in access logs, browser history,
    and Referer headers. Mitigated by HTTPS + the access token's short lifetime;
    the hardened alternative is a dedicated short-lived, stream-scoped token.
    """
    user = await _load_user_from_token(header_token or access_token, db)
    if not user.is_active:
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="Inactive user")
    return user

StreamUser = Annotated[User, Depends(get_stream_user)]


async def get_current_admin(current_user: CurrentUser) -> User:
    if not current_user.is_admin:
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="Admin only")
    return current_user

CurrentAdmin = Annotated[User, Depends(get_current_admin)]