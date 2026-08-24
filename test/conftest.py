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

os.environ.setdefault("REFRESH_TOKEN_REUSE_GRACE_SECONDS", "0")

# 測試要用 /orders/{id}/pay 的模擬付款,所以顯式打開它 —— 生產預設是關的,
# 而 Settings 的 validator 會拒絕在 DEBUG=False 下啟用。
os.environ.setdefault("DEBUG", "True")
os.environ.setdefault("ENABLE_MOCK_PAYMENT", "True")


import pytest
import pytest_asyncio
from unittest.mock import AsyncMock, MagicMock
from httpx import AsyncClient, ASGITransport

from app.main import app
from app.core.config import get_settings
from app.core.redis import create_redis_client
from app.db.base import Base
from app.db.session import engine
from app.db.session import AsyncSessionLocal
from app.core.config import get_settings

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

@pytest.fixture(autouse=True)
def _fast_password_hashing(request):
    """把密碼雜湊換成快速替身 —— 除非測試標了 `real_hashing`。

    Argon2 是**刻意**慢的(實測每次雜湊 221ms、驗證 229ms),而整套測試有 69% 的
    時間花在它上面(166 次呼叫 = 39 秒)。更糟的是它是純 CPU,所以主機一忙整套的
    耗時就翻倍 —— 任何基於測試耗時的判斷都變得不可靠。

    測試絕大多數需要的不是「KDF 真的很難暴力破解」,而是「雜湊會被呼叫、驗證會
    通過或失敗」。真正的 Argon2 由 test_password_hashing.py 單獨驗證(標記
    real_hashing),所以覆蓋率沒有洞。

    patch 打在 `ticket_secrets` 而不是 `app.core.security`:後者的函式可能已被別的
    模組 from-import 綁走,而 security.py 是用 `ticket_secrets.hash_password(...)`
    的屬性查找 —— 在來源打 patch 才能全域生效。
    """
    if request.node.get_closest_marker("real_hashing"):
        yield
        return

    import hashlib
    import ticket_secrets

    real_hash = ticket_secrets.hash_password
    real_verify = ticket_secrets.verify_password
    prefix = "faketest$"

    def fake_hash(password: str) -> str:
        return prefix + hashlib.sha256(password.encode()).hexdigest()

    def fake_verify(password: str, hashed: str) -> bool:
        # 沒有前綴的是真 hash(例如 fixture 直接塞的)—— 交回真函式,
        # 免得替身把「這個 hash 我不認識」誤判成密碼錯誤。
        if not hashed.startswith(prefix):
            return real_verify(password, hashed)
        return fake_hash(password) == hashed

    ticket_secrets.hash_password = fake_hash
    ticket_secrets.verify_password = fake_verify
    try:
        yield
    finally:
        ticket_secrets.hash_password = real_hash
        ticket_secrets.verify_password = real_verify


@pytest.fixture(scope="session")
def anyio_backend():
    """Force pytest-anyio to use asyncio (not trio)."""
    return "asyncio"


@pytest_asyncio.fixture
async def client():
    # lifespan 不會在 ASGITransport 下執行,手動接上 app.state.redis
    # (登入端點寫稽核事件需要它),用完關掉避免連線洩漏。
    app.state.redis = create_redis_client(get_settings().REDIS_URL)
    # webhook/payment endpoints resolve get_stripe -> app.state.stripe; lifespan
    # doesn't run under ASGITransport, so stub it with a mock (refunds recorded).
    app.state.stripe = MagicMock()
    app.state.stripe.v1.refunds.create_async = AsyncMock(
        return_value=MagicMock(id="re_test", status="succeeded")
    )
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
    """大多數測試會重複登入/註冊,開著限流的話它們會為了無關的理由變紅。

    要驗限流本身的測試改 request `rate_limiting` fixture(在下面)—— 不要在測試裡
    自己翻旗標,那就是同一個決定被寫在五個地方的開端。

    直接改 Settings 實例而不是設環境變數:`get_settings` 是 lru_cache 的,環境變數
    在 import 之後才改沒有效果。存原值再還原,而不是無條件設回 True —— 否則跟
    `rate_limiting` 疊在一起時會把狀態留給下一個測試。
    """
    settings = get_settings()
    original = settings.RATE_LIMIT_ENABLED
    settings.RATE_LIMIT_ENABLED = False
    yield
    settings.RATE_LIMIT_ENABLED = original


@pytest.fixture
def rate_limiting():
    """把限流打開(蓋過 autouse 的 `_disable_rate_limit`)。回傳 settings 讓測試
    可以順手調上限。"""
    settings = get_settings()
    settings.RATE_LIMIT_ENABLED = True
    yield settings
    settings.RATE_LIMIT_ENABLED = False

@pytest_asyncio.fixture
async def published_event(db, redis):
    from datetime import datetime, timedelta, timezone
    from app.models.event import Event, EventStatus
    from app.services.inventory import set_initial_stock

    now = datetime.now(timezone.utc)
    event = Event(
        name="Test Concert", venue="Test Arena",
        starts_at=now + timedelta(days=30),
        ends_at=now + timedelta(days=30, hours=3),
        sale_starts_at=now - timedelta(days=1),   # 已開賣
        sale_ends_at=now + timedelta(days=1),      # 未結束
        total_seats=5, price_cents=1500,
        status=EventStatus.PUBLISHED,
    )
    db.add(event)
    await db.commit()                              # commit 後端點的 session 才看得到
    await set_initial_stock(redis, event_id=event.id, total_seats=event.total_seats)
    return event


@pytest_asyncio.fixture
async def drain_orders(redis):
    """Run the order-stream consumer once, like the worker would.

    The group is normally created in worker startup(); we create it here
    (idempotently) so tests can drain the stream after posting orders.
    """
    from app.worker import consume_order_intents, ORDER_CONSUMER_GROUP
    from app.services.inventory import ORDER_STREAM_KEY

    async def _drain():
        try:
            await redis.xgroup_create(
                ORDER_STREAM_KEY, ORDER_CONSUMER_GROUP, id="0", mkstream=True
            )
        except Exception as exc:
            if "BUSYGROUP" not in str(exc):
                raise
        await consume_order_intents({"redis_client": redis})

    return _drain