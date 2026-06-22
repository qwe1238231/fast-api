"""Pytest fixtures shared across all tests."""
import pytest
import pytest_asyncio
from httpx import AsyncClient, ASGITransport

from app.main import app
from app.core.config import get_settings
from app.core.redis import create_redis_client


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
            base_url="http://test",
        ) as ac:
            yield ac
    finally:
        await app.state.redis.aclose()
