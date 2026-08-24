"""比對「migration 建出來的索引」與「model metadata 建出來的索引」。

用法: python -m app.scripts.check_index_drift        (退出碼非零代表有漂移)

**為什麼 `alembic check` 不夠。** 它比對欄位、約束、外鍵的 ON DELETE,但**看不到
索引定義的細節**。實測確認至少漏掉:

    postgresql_include=[...]   把 model 的 INCLUDE 拿掉,alembic check 照樣說乾淨

同一類還有 opclass、排序方向(DESC / NULLS LAST)、表達式索引的內容。這些漂移的
共同點是**不會有任何測試變紅**:測試走 metadata.create_all,所以它們看到的永遠是
model 那一份;而正式環境跑的是 migration 那一份。兩份不一致時,你在本機量到的
查詢計畫跟線上跑的不是同一個東西。

做法是拿 Postgres 自己的 `pg_get_indexdef` 當共同語言:把 metadata 建進一個乾淨的
參考資料庫,兩邊各自問一次,逐字比對。這樣連 opclass 這種很難自己實作比對的東西
也一併涵蓋 —— 因為根本不需要自己實作,是資料庫在說。

參考資料庫必須是**另一個 database** 而不是另一個 schema:索引名在 Postgres 是
schema 層級全域的,同名索引沒辦法在同一個 database 裡並存。
"""
import asyncio
import os
import sys

from sqlalchemy import text
from sqlalchemy.ext.asyncio import create_async_engine

from app.core.config import get_settings
from app.db.base import Base
from app import models  # noqa: F401  — 讓所有 mapper 註冊進 metadata

#: 不參與比對的表。alembic 自己的簿記表不在 model 裡,永遠會被報成差異。
_IGNORED_TABLES = frozenset({"alembic_version"})

_INDEX_QUERY = text(
    """
    SELECT t.relname, pg_get_indexdef(i.indexrelid)
    FROM pg_index i
    JOIN pg_class c ON c.oid = i.indexrelid
    JOIN pg_class t ON t.oid = i.indrelid
    JOIN pg_namespace n ON n.oid = t.relnamespace
    WHERE n.nspname = 'public'
      -- 子分區的索引是父表索引自動下推的副本,而分區本身是 worker 在執行期建的,
      -- 不在 metadata 裡。跟著父表比就好。
      AND NOT c.relispartition
      AND NOT t.relispartition
    """
)


async def _index_defs(url: str) -> dict[str, set[str]]:
    engine = create_async_engine(url, pool_pre_ping=False)
    try:
        async with engine.connect() as conn:
            rows = (await conn.execute(_INDEX_QUERY)).all()
    finally:
        await engine.dispose()

    defs: dict[str, set[str]] = {}
    for table, indexdef in rows:
        if table in _IGNORED_TABLES:
            continue
        defs.setdefault(table, set()).add(indexdef)
    return defs


async def _build_reference(admin_url: str, ref_url: str, ref_name: str) -> None:
    """把 model metadata 建進一個全新的參考資料庫。"""
    admin = create_async_engine(admin_url, isolation_level="AUTOCOMMIT")
    try:
        async with admin.connect() as conn:
            await conn.execute(text(f'DROP DATABASE IF EXISTS "{ref_name}"'))
            await conn.execute(text(f'CREATE DATABASE "{ref_name}"'))
    finally:
        await admin.dispose()

    engine = create_async_engine(ref_url, pool_pre_ping=False)
    try:
        async with engine.begin() as conn:
            await conn.run_sync(Base.metadata.create_all)
    finally:
        await engine.dispose()


async def _drop_reference(admin_url: str, ref_name: str) -> None:
    admin = create_async_engine(admin_url, isolation_level="AUTOCOMMIT")
    try:
        async with admin.connect() as conn:
            await conn.execute(text(f'DROP DATABASE IF EXISTS "{ref_name}"'))
    finally:
        await admin.dispose()


def _report(migrated: dict[str, set[str]], reference: dict[str, set[str]]) -> list[str]:
    problems: list[str] = []
    for table in sorted(set(migrated) | set(reference)):
        only_db = migrated.get(table, set()) - reference.get(table, set())
        only_model = reference.get(table, set()) - migrated.get(table, set())
        for indexdef in sorted(only_db):
            problems.append(f"  [只在資料庫裡] {indexdef}")
        for indexdef in sorted(only_model):
            problems.append(f"  [只在 model 裡] {indexdef}")
    return problems


async def main() -> int:
    url = get_settings().DATABASE_URL
    base, _, db_name = url.rpartition("/")
    ref_name = f"{db_name}_indexref"
    ref_url = f"{base}/{ref_name}"
    admin_url = f"{base}/postgres"

    await _build_reference(admin_url, ref_url, ref_name)
    try:
        migrated = await _index_defs(url)
        reference = await _index_defs(ref_url)
    finally:
        await _drop_reference(admin_url, ref_name)

    problems = _report(migrated, reference)
    if problems:
        print("索引定義漂移 —— migration 與 model 對不上:", file=sys.stderr)
        print("\n".join(problems), file=sys.stderr)
        print(
            "\n這一類 alembic check 抓不到(它不比對 INCLUDE / opclass / 排序方向),"
            "\n而測試走 metadata.create_all,所以也不會紅。",
            file=sys.stderr,
        )
        return 1

    print(f"索引定義一致({sum(len(v) for v in migrated.values())} 個索引)")
    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
