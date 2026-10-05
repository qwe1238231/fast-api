"""比對「migration 建出來的 schema」與「model metadata 建出來的 schema」。

用法: python -m app.scripts.check_schema_drift       (退出碼非零代表有漂移)

目前涵蓋 `alembic check` 看不到的三類:

    索引定義        postgresql_include、opclass、排序方向、表達式的內容
    表的儲存參數    reloptions,也就是 per-table 的 autovacuum 調校
    trigger         定義與它呼叫的函式本體(seat_holds ↔ orders 的一致性靠它)

**為什麼 `alembic check` 不夠。** 它比對欄位、約束、外鍵的 ON DELETE,但這兩類都
不在它的比對範圍內。實測確認:把 model 的 `postgresql_include` 拿掉,alembic check
照樣回報「No new upgrade operations detected」。

這些漂移的共同點是**不會有任何測試變紅**:測試走 metadata.create_all,所以它們看到
的永遠是 model 那一份;而正式環境跑的是 migration 那一份。兩份不一致時,你在本機
量到的查詢計畫跟線上跑的不是同一個東西 —— 而 orders 的 autovacuum 設定正是「差一個
參數就讓覆蓋索引失效」的那種東西。

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


_RELOPTIONS_QUERY = text(
    """
    SELECT c.relname, unnest(coalesce(c.reloptions, '{}'))
    FROM pg_class c
    JOIN pg_namespace n ON n.oid = c.relnamespace
    WHERE n.nspname = 'public'
      AND c.relkind IN ('r', 'p')
      AND NOT c.relispartition
    """
)


_TRIGGER_QUERY = text(
    """
    SELECT t.relname,
           pg_get_triggerdef(g.oid) || E'\\n' || pg_get_functiondef(g.tgfoid)
    FROM pg_trigger g
    JOIN pg_class t ON t.oid = g.tgrelid
    JOIN pg_namespace n ON n.oid = t.relnamespace
    WHERE n.nspname = 'public'
      AND NOT g.tgisinternal        -- 外鍵的 RI 檢查也是 trigger,那些不歸這裡管
      AND NOT t.relispartition
    """
)


async def _snapshot(
        url: str,
) -> tuple[dict[str, set[str]], dict[str, set[str]], dict[str, set[str]]]:
    """回傳 (每張表的索引定義, 每張表的 reloptions, 每張表的 trigger+函式本體)。"""
    engine = create_async_engine(url, pool_pre_ping=False)
    try:
        async with engine.connect() as conn:
            index_rows = (await conn.execute(_INDEX_QUERY)).all()
            option_rows = (await conn.execute(_RELOPTIONS_QUERY)).all()
            trigger_rows = (await conn.execute(_TRIGGER_QUERY)).all()
    finally:
        await engine.dispose()

    def bucket(rows) -> dict[str, set[str]]:
        out: dict[str, set[str]] = {}
        for table, item in rows:
            if table not in _IGNORED_TABLES:
                out.setdefault(table, set()).add(item)
        return out

    return bucket(index_rows), bucket(option_rows), bucket(trigger_rows)


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


def _report(
        kind: str,
        migrated: dict[str, set[str]],
        reference: dict[str, set[str]],
) -> list[str]:
    problems: list[str] = []
    for table in sorted(set(migrated) | set(reference)):
        only_db = migrated.get(table, set()) - reference.get(table, set())
        only_model = reference.get(table, set()) - migrated.get(table, set())
        for item in sorted(only_db):
            problems.append(f"  [{kind}] [只在資料庫裡] {item}")
        for item in sorted(only_model):
            problems.append(f"  [{kind}] [只在 model 裡] {item}")
    return problems


async def main() -> int:
    url = get_settings().DATABASE_URL
    base, _, db_name = url.rpartition("/")
    ref_name = f"{db_name}_indexref"
    ref_url = f"{base}/{ref_name}"
    admin_url = f"{base}/postgres"

    await _build_reference(admin_url, ref_url, ref_name)
    try:
        db_indexes, db_options, db_triggers = await _snapshot(url)
        ref_indexes, ref_options, ref_triggers = await _snapshot(ref_url)
    finally:
        await _drop_reference(admin_url, ref_name)

    problems = (
        _report("索引", db_indexes, ref_indexes)
        + _report("儲存參數", db_options, ref_options)
        + _report("trigger", db_triggers, ref_triggers)
    )
    if problems:
        print("schema 漂移 —— migration 與 model 對不上:", file=sys.stderr)
        print("\n".join(problems), file=sys.stderr)
        print(
            "\n這一類 alembic check 抓不到(它不比對 INCLUDE / opclass / 排序方向 /"
            " reloptions / trigger),\n而測試走 metadata.create_all,所以也不會紅。",
            file=sys.stderr,
        )
        return 1

    print(
        f"schema 一致({sum(len(v) for v in db_indexes.values())} 個索引、"
        f"{sum(len(v) for v in db_options.values())} 個儲存參數、"
        f"{sum(len(v) for v in db_triggers.values())} 個 trigger)"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
