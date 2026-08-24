import asyncio
import os
import sys
from logging.config import fileConfig

from sqlalchemy import pool, text
from sqlalchemy.ext.asyncio import create_async_engine
from alembic import context

# 把專案根目錄加進 Python 搜尋路徑
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from app.core.config import get_settings
from app.db.base import Base
from app import models  # noqa: F401

# Alembic config 設定（集中在一起）
DATABASE_URL = get_settings().DATABASE_URL
config = context.config
config.set_main_option("sqlalchemy.url", DATABASE_URL)

if config.config_file_name is not None:
    fileConfig(config.config_file_name)

target_metadata = Base.metadata


def _make_include_object(connection):
    """略過所有子分區。

    audit_logs 是按月分區的(見 app/models/audit_log.py),而子分區 —— audit_logs
    _2026_08、audit_logs_default 之類 —— 是**由 worker 的 cron 在執行期建立與刪除**
    的,不在 model metadata 裡。不過濾的話 autogenerate 會把它們全部當成「資料庫
    有而 model 沒有」的孤兒表,每次 `alembic check` 都吵著要 DROP,而下個月又會
    冒出新的一批。

    用 pg_class.relispartition 判斷而不是比對表名前綴:命名規則之後可能改,
    「它是不是一個分區」則是資料庫自己知道的事實。

    **查詢一定要延遲到真的被呼叫的時候。** 在 configure() 當下就跑一次 SELECT 的話,
    SQLAlchemy 2.0 的 commit-as-you-go 連線會隱式開一個交易,alembic 自己的
    begin_transaction() 就變成不會 commit 的巢狀空殼 —— 每一支 migration 都會
    exit 0、印出 "Running migration"、然後整個回滾。而 include_object 只有
    autogenerate / check 會呼叫,upgrade 與 downgrade 根本用不到它。
    """
    cache: set[str] | None = None

    def partitions() -> set[str]:
        nonlocal cache
        if cache is None:
            cache = {
                row[0]
                for row in connection.execute(
                    text("SELECT relname FROM pg_class WHERE relispartition")
                )
            }
        return cache

    def include_object(object_, name, type_, reflected, compare_to) -> bool:
        if type_ == "table":
            return name not in partitions()
        if type_ == "index":
            # 分區上的索引是父表索引自動下推的副本,跟著父表管就好。
            return getattr(object_.table, "name", None) not in partitions()
        return True

    return include_object


def do_run_migrations(connection) -> None:
    context.configure(
        connection=connection,
        target_metadata=target_metadata,
        compare_type=True,
        include_object=_make_include_object(connection),
    )
    with context.begin_transaction():
        context.run_migrations()


def run_migrations_offline() -> None:
    context.configure(
        url=DATABASE_URL,  # ✅
        target_metadata=target_metadata,
        literal_binds=True,
        dialect_opts={"paramstyle": "named"},
        compare_type=True,
    )
    with context.begin_transaction():
        context.run_migrations()

async def run_async_migrations() -> None:
    connectable = create_async_engine(
        DATABASE_URL,  # ✅
        poolclass=pool.NullPool,
    )
    async with connectable.connect() as connection:
        await connection.run_sync(do_run_migrations)
    await connectable.dispose()


def run_migrations_online() -> None:
    asyncio.run(run_async_migrations())


if context.is_offline_mode():
    run_migrations_offline()
else:
    run_migrations_online()