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
    # (statement_timeout is deliberately NOT set here — it would kill the worker's
    #  legitimately-long cron queries; apply it per-request in the API if wanted.)
    # application_name tags pg_stat_activity so the three process types are
    # distinguishable when diagnosing a connection leak under load.
    connect_args={
        "server_settings": {
            "application_name": os.getenv("APP_COMPONENT", "ticket-api"),
            "idle_in_transaction_session_timeout": "15000",   # ms
            "lock_timeout": "3000",                            # ms
        },
    },
    )
AsyncSessionLocal = async_sessionmaker(engine, expire_on_commit=False)

async def get_db() -> AsyncGenerator[AsyncSession]:
    async with AsyncSessionLocal() as session:
        yield session