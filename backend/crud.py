from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy import select
import models,schemas,security

async def get_user_by_username(db:AsyncSession,username:str):
    result = await db.execute(select(models.User).where(models.User.username==username))
    return result.scalars().first()

async def create_user(db:AsyncSession,user:schemas.UserCreate):
    hashed_password = security.get_password_hash(user.password)
    db_user = models.User(username=user.username,hashed_password=hashed_password)
    db.add(db_user)
    await db.commit()
    await db.refresh(db_user)
    return db_user






