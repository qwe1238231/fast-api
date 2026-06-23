"""Pytest fixtures shared across all tests."""
import os
os.environ.setdefault(
    "DATABASE_URL",
    "postgresql+asyncpg://justinhu@localhost:5432/testdb_test",
)
os.environ.setdefault(
    "REDIS_URL",
    "redis://localhost:6380/15",   # db 15,跟開發的 db 0 完全隔開
)

import pytest
import pytest_asyncio
from httpx import AsyncClient, ASGITransport

from app.main import app
from app.core.config import get_settings
from app.core.redis import create_redis_client
from app.db.base import Base
from app.db.session import engine
from app.db.session import AsyncSessionLocal
from app.api.deps import limiter

from sqlalchemy import text


@pytest_asyncio.fixture(scope="session", autouse=True)
async def _setup_schema():
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)   # 從 ORM model 直接建表
    yield
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.drop_all)

@pytest_asyncio.fixture(autouse=True)
async def _clean_tables():
    yield   # 先讓測試跑
    async with engine.begin() as conn:
        table_names = ", ".join(Base.metadata.tables)
        await conn.execute(
            text(f"TRUNCATE {table_names} RESTART IDENTITY CASCADE")
        )

@pytest.fixture(scope="session")
def anyio_backend():
    """Force pytest-anyio to use asyncio (not trio)."""
    return "asyncio"


@pytest_asyncio.fixture
async def client():
    # lifespan 不會在 ASGITransport 下執行,手動接上 app.state.redis
    # (登入端點寫稽核事件需要它),用完關掉避免連線洩漏。
    app.state.redis = create_redis_client(get_settings().REDIS_URL)
    try:
        async with AsyncClient(
            transport=ASGITransport(app=app),
            base_url="https://test",
        ) as ac:
            yield ac
    finally:
        await app.state.redis.aclose()

@pytest_asyncio.fixture(autouse=True)
async def _clean_redis():
    yield
    r = create_redis_client(get_settings().REDIS_URL)
    await r.flushdb()      # 只清 db 15
    await r.aclose()

@pytest_asyncio.fixture
async def db():
    async with AsyncSessionLocal() as session:
        yield session

@pytest_asyncio.fixture
async def redis():
    r = create_redis_client(get_settings().REDIS_URL)
    try:
        yield r
    finally:
        await r.aclose()

@pytest.fixture(autouse=True)
def _disable_rate_limit():
    limiter.enabled = False
    yield
    limiter.enabled = True