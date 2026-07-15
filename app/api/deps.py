from typing import Annotated

import jwt
from fastapi import Depends, HTTPException, status, Request
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

async def get_current_user(
        token: Annotated[str, Depends(oauth2_scheme)],
        db: DbSession,
) -> User:
    credentials_exception = HTTPException(
        status_code=status.HTTP_401_UNAUTHORIZED,
        detail="Could not validate credentials",
        headers={"WWW-Authenticate": "Bearer"},
    )
    settings = get_settings()
    try:
        raw_payload = jwt.decode(
            token,
            settings.SECRET_KEY,
            algorithms=[settings.ALGORITHM],
        )
        payload = TokenPayload(**raw_payload)
    except (InvalidTokenError, ValidationError):
        raise credentials_exception
    
    user = await get_user_by_username(db, username=payload.sub)
    if user is None:
        raise credentials_exception
    return user


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

async def get_current_admin(current_user: CurrentUser) -> User:
    if not current_user.is_admin:
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="Admin only")
    return current_user

CurrentAdmin = Annotated[User, Depends(get_current_admin)]