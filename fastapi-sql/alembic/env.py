import asyncio
from logging.config import fileConfig

from sqlalchemy import pool
from sqlalchemy.ext.asyncio import async_engine_from_config

from alembic import context

# ---------------------------------------------------------
# 1. 加入這段：讓 Python 找得到你的專案路徑
import sys
import os
# 1. 取得 env.py 目前的絕對路徑
current_path = os.path.dirname(os.path.abspath(__file__))
# 2. 取得上一層目錄 (專案根目錄)
root_path = os.path.dirname(current_path)
# 3. 強制將根目錄加入 Python 搜尋路徑的第一順位
sys.path.insert(0, root_path)

# 2. 匯入你的 Database 設定和 Base
from database import DATABASE_URL, Base
# 3. 重要！匯入所有 models 讓 Alembic 能偵測到它們
from models import Book  # 匯入所有的 model 類別
# ---------------------------------------------------------

config = context.config

# ---------------------------------------------------------
# 4. 覆蓋 alembic.ini 的設定，直接使用 Python 裡的連線字串
config.set_main_option("sqlalchemy.url", DATABASE_URL)
# ---------------------------------------------------------

if config.config_file_name is not None:
    fileConfig(config.config_file_name)

# ---------------------------------------------------------
# 5. 指定 target_metadata 為你的 Base
target_metadata = Base.metadata
# ---------------------------------------------------------

def run_migrations_offline() -> None:
    """Run migrations in 'offline' mode."""
    url = config.get_main_option("sqlalchemy.url")
    context.configure(
        url=url,
        target_metadata=target_metadata,
        literal_binds=True,
        dialect_opts={"paramstyle": "named"},
    )

    with context.begin_transaction():
        context.run_migrations()

async def run_async_migrations() -> None:
    """In this scenario we need to create an Engine
    and associate a connection with the context.
    """

    connectable = async_engine_from_config(
        config.get_section(config.config_ini_section, {}),
        prefix="sqlalchemy.",
        poolclass=pool.NullPool,
    )

    async with connectable.connect() as connection:
        await connection.run_sync(do_run_migrations)

    await connectable.dispose()

def do_run_migrations(connection):
    context.configure(connection=connection, target_metadata=target_metadata)

    with context.begin_transaction():
        context.run_migrations()

def run_migrations_online() -> None:
    """Run migrations in 'online' mode."""
    asyncio.run(run_async_migrations())

if context.is_offline_mode():
    run_migrations_offline()
else:
    run_migrations_online()