from datetime import datetime, timedelta, timezone
from uuid import UUID, uuid4

from sqlalchemy import select, update, delete, or_
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.refresh_token import RefreshToken
from app.core.security import generate_refresh_token ,hash_refresh_token


async def issue_refresh_token(
        db: AsyncSession,
        *,
        user_id: int,
        sliding_lifetime: timedelta,
        absolute_expire_lifetime: timedelta,
        parent: RefreshToken | None = None,
        user_agent: str | None = None,
        ip_address: str | None = None,
) -> tuple[str, RefreshToken]:
    now = datetime.now(timezone.utc)

    if parent is None:
        family_id = uuid4()
        absolute_expires_at = now + absolute_expire_lifetime
        parent_id = None
    else:
        family_id = parent.family_id
        absolute_expires_at = parent.absolute_expires_at
        parent_id = parent.id

    sliding_expire_at = min(now + sliding_lifetime, absolute_expires_at)
    plaintext, token_hash = generate_refresh_token()

    token = RefreshToken(
        user_id=user_id,
        token_hash=token_hash,
        family_id=family_id,
        parent_id=parent_id,
        expires_at=sliding_expire_at,
        absolute_expires_at=absolute_expires_at,
        user_agent=user_agent,
        ip_address=ip_address,
         
    )
    db.add(token)
    await db.flush()
    return plaintext, token


async def get_refresh_token_for_update(
        db: AsyncSession,
        plaintext_token: str,
) -> RefreshToken | None:
    token_hash = hash_refresh_token(plaintext_token)
    stmt = (
        select(RefreshToken)
        .where(RefreshToken.token_hash == token_hash)
        .with_for_update()
    )
    result = await db.execute(stmt)
    return result.scalar_one_or_none()


async def mark_token_used(
        db: AsyncSession,
        token: RefreshToken,    
) -> None:
    if token.used_at is None:
        token.used_at = datetime.now(timezone.utc)
        await db.flush()


async def revoke_family(
        db: AsyncSession,
        family_id: UUID,
) -> None:
    stmt = (
        update(RefreshToken)
        .where(RefreshToken.family_id == family_id)
        .where(RefreshToken.revoked_at.is_(None))
        .values(revoked_at=datetime.now(timezone.utc))
    )
    await db.execute(stmt) 


async def revoke_all_for_user(
        db: AsyncSession,
        user_id: int,
) -> None:
    stmt = (
        update(RefreshToken)
        .where(RefreshToken.user_id == user_id)
        .where(RefreshToken.revoked_at.is_(None))
        .values(revoked_at=datetime.now(timezone.utc))
    )
    await db.execute(stmt)

async def purge_expired(db: AsyncSession) -> int:
    """Delete refresh_tokens rows past their useful lifetime.
    
    Returns the number of deleted rows.
    """
    now = datetime.now(timezone.utc)
    revoked_cutoff = now - timedelta(days=30)
    stmt = delete(RefreshToken).where(
        or_(
            RefreshToken.absolute_expires_at < now,
            RefreshToken.revoked_at < revoked_cutoff,
        )
    )
    result = await db.execute(stmt)
    return result.rowcount