"""建立或提升一個 admin 使用者。

用法: python -m app.scripts.create_admin <username> <password>
"""
import asyncio
import sys

from sqlalchemy import select

from app.core.security import get_password_hash
from app.db.session import AsyncSessionLocal
from app.models.user import User


async def create_admin(username: str, password: str) -> None:
    async with AsyncSessionLocal() as db:
        user = await db.scalar(select(User).where(User.username == username))
        if user is None:
            db.add(User(
                username=username,
                hashed_password=get_password_hash(password),
                is_admin=True,
            ))
            action = "created"
        else:
            user.is_admin = True
            action = "promoted"
        await db.commit()
        print(f"User '{username}' {action} as admin.")

if __name__ == "__main__":
    if len(sys.argv) != 3:
        print("Usage: python -m app.scripts.create_admin <username> <password>")
        sys.exit(1)
    asyncio.run(create_admin(sys.argv[1], sys.argv[2]))