import os

from sqlalchemy.ext.asyncio import AsyncSession, create_async_engine, async_sessionmaker
from collections.abc import AsyncGenerator
from app.core.config import get_settings

settings = get_settings()

_APP_NAME = os.getenv("APP_COMPONENT", "ticket-api")

# asyncpg server-side session settings, applied to every connection at startup.
# Safe to apply to ALL process types (API + worker + consumer): these only bound
# PATHOLOGICAL states, never a legitimately-running statement —
#   idle_in_transaction_session_timeout: reap a session left mid-transaction (e.g. a
#     request cancelled / a txn pinned across a stalled Redis await) so it can't hold
#     row locks + the vacuum xmin horizon forever;
#   lock_timeout: don't block indefinitely on a row lock (bounds the refresh-token
#     SELECT ... FOR UPDATE and any contended UPDATE).
#   statement_timeout: only when DB_STATEMENT_TIMEOUT_MS is set — see below.
# application_name tags pg_stat_activity so the process types (and the cache pool,
# below) are distinguishable when diagnosing a connection leak under load.
_SERVER_SETTINGS: dict[str, str] = {
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
}

engine = create_async_engine(
    settings.DATABASE_URL,
    echo=settings.SQL_ECHO,
    pool_size=settings.DB_POOL_SIZE,
    max_overflow=settings.DB_MAX_OVERFLOW,
    pool_pre_ping=settings.DB_POOL_PRE_PING,
    pool_recycle=settings.DB_POOL_RECYCLE,
    connect_args={"server_settings": {**_SERVER_SETTINGS, "application_name": _APP_NAME}},
)
AsyncSessionLocal = async_sessionmaker(engine, expire_on_commit=False)

#: 快取重算專用的小 pool —— bulkhead。
#:
#: event meta 的重算跑在 singleflight 的獨立 task 上,而**等它的每一個請求都握著主 pool
#: 的連線**(auth 讀過 user 之後,session 的交易到 request 結束才收,連線也跟著被握到底)。
#: loader 若也向主 pool 要連線,主 pool 一滿就是互等:請求等 flight、flight 等連線、
#: 連線在請求手上。2026-09-15 對 4 workers 打 300 併發冷 key 實測:149 個 504、DB 讀 0 次。
#: 一般的 pool 排隊只是慢,這種是死鎖 —— 而且只在最需要快取的那一刻(開賣瞬間)發生。
#:
#: 拆成獨立 pool 之後「等的人」和「做的人」不再共用資源。
#:
#: 尺寸是 **1 條、不溢位**,而且這是被連線預算逼出來的:api 部署時 200% 尖峰 × 每任務
#: 的 cache pool 全部算進 RDS 的 max_connections(test_deploy_pipeline 的預算測試),
#: 1+2 會超 20 條,1+0 剛好貼著上限。1 條夠用的前提是重算是毫秒級(PK 點查 + 一個
#: JOIN):同 process 同時有兩個 key 冷掉會序列化,第二個多等幾毫秒。pool_timeout 是
#: 保險絲 —— 前一個重算卡在慢 DB 上超過 5 秒,第二個 flight 以例外收場、下一個呼叫者
#: 重試,比等 30 秒好;這裡等不到連線只可能是自己塞住。
#: pre_ping 一定開:一分鐘才用一次的連線,最容易被 NAT / idle timeout 悄悄收掉。
#:
#: 連線預算:每個 api 任務多 CACHE_POOL_SIZE + CACHE_POOL_MAX_OVERFLOW 條(見 config 的預算
#: 註解)。只有 api 的請求路徑會呼叫 get_event_meta,worker / consumer 雖然 import 了這個
#: 模組,pool 是 lazy 的,一條都不會開 —— 預算算術據此只算 api。具名常數而不是字面值:
#: 預算測試要 import 它們,改這裡測試才會跟著動。
CACHE_POOL_SIZE = 1
CACHE_POOL_MAX_OVERFLOW = 0
cache_engine = create_async_engine(
    settings.DATABASE_URL,
    echo=settings.SQL_ECHO,
    pool_size=CACHE_POOL_SIZE,
    max_overflow=CACHE_POOL_MAX_OVERFLOW,
    pool_timeout=5,
    pool_pre_ping=True,
    pool_recycle=settings.DB_POOL_RECYCLE,
    connect_args={"server_settings": {**_SERVER_SETTINGS, "application_name": f"{_APP_NAME}-cache"}},
)
CacheSessionLocal = async_sessionmaker(cache_engine, expire_on_commit=False)


async def get_db() -> AsyncGenerator[AsyncSession]:
    async with AsyncSessionLocal() as session:
        yield session
