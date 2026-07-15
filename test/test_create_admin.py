import pytest
from sqlalchemy import select

from app.core.security import get_password_hash
from app.models.user import User
from app.scripts.create_admin import create_admin


@pytest.mark.asyncio
async def test_create_admin_creates_new_user(db):
    await create_admin("newadmin", "secret123")

    user = await db.scalar(select(User).where(User.username == "newadmin"))
    assert user is not None
    assert user.is_admin is True


@pytest.mark.asyncio
async def test_create_admin_promotes_existing_user(db):
    bob = User(username="bob", hashed_password=get_password_hash("x"), is_admin=False)
    db.add(bob)
    await db.commit()

    await create_admin("bob", "ignored")

    await db.refresh(bob)          # create_admin 在另一個 session commit,要 refresh 才看得到
    assert bob.is_admin is True
