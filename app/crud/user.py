from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession
from app.schemas.user import UserCreate
from app.core.security import hash_password_async
from app.models import User



async def get_user_by_username(
        db: AsyncSession,
        username: str,
) -> User | None:
    stmt = select(User).where(User.username == username)
    result = await db.execute(stmt)
    return result.scalar_one_or_none()



async def get_user_by_id(
        db: AsyncSession,
        user_id: int,
) -> User | None:
    return await db.get(User, user_id)


async def create_user(
        db: AsyncSession,
        user_in: UserCreate,
) -> User:
    user = User(
        username=user_in.username,
        hashed_password=await hash_password_async(
            user_in.password.get_secret_value()
        ),
    )
    db.add(user)
    await db.commit()
    await db.refresh(user)
    return user