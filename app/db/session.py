import os

from sqlalchemy.ext.asyncio import AsyncSession, create_async_engine, async_sessionmaker
from collections.abc import AsyncGenerator
from app.core.config import get_settings

settings = get_settings()

engine = create_async_engine(
    settings.DATABASE_URL,
    echo=settings.SQL_ECHO,
    pool_size=settings.DB_POOL_SIZE,
    max_overflow=settings.DB_MAX_OVERFLOW,
    pool_pre_ping=settings.DB_POOL_PRE_PING,
    pool_recycle=settings.DB_POOL_RECYCLE,
    # asyncpg server-side session settings, applied to every connection at startup.
    # Safe to apply to ALL process types (API + worker + consumer): these only bound
    # PATHOLOGICAL states, never a legitimately-running statement —
    #   idle_in_transaction_session_timeout: reap a session left mid-transaction (e.g. a
    #     request cancelled / a txn pinned across a stalled Redis await) so it can't hold
    #     row locks + the vacuum xmin horizon forever;
    #   lock_timeout: don't block indefinitely on a row lock (bounds the refresh-token
    #     SELECT ... FOR UPDATE and any contended UPDATE).
    #   statement_timeout: only when DB_STATEMENT_TIMEOUT_MS is set — see below.
    # application_name tags pg_stat_activity so the three process types are
    # distinguishable when diagnosing a connection leak under load.
    connect_args={
        "server_settings": {
            "application_name": os.getenv("APP_COMPONENT", "ticket-api"),
            "idle_in_transaction_session_timeout": "15000",   # ms
            "lock_timeout": "3000",                            # ms
            # statement_timeout 預設**不設**(0),因為 worker 的對帳/漂移 cron 有合法的
            # 長查詢,而部署時的 `alembic upgrade` 也用 worker 的 task def 跑 —— 一個被
            # 砍掉的 ALTER TABLE 比一個慢查詢糟得多。所以只有 api 的 task def 打開它。
            #
            # 掛在**連線**上而不是每個請求下一次 `SET LOCAL`:所有 API 請求要的值都一樣,
            # 而每請求一次 SET 就是每請求多一趟 round-trip —— 在搶票尖峰上那是純粹的浪費。
            # 需要逐端點調整時再改成 per-request,今天沒有這個需求。
            **(
                {"statement_timeout": str(settings.DB_STATEMENT_TIMEOUT_MS)}
                if settings.DB_STATEMENT_TIMEOUT_MS > 0
                else {}
            ),
        },
    },
    )
AsyncSessionLocal = async_sessionmaker(engine, expire_on_commit=False)

async def get_db() -> AsyncGenerator[AsyncSession]:
    async with AsyncSessionLocal() as session:
        yield session