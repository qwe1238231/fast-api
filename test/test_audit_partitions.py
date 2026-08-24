"""audit_logs 的按月分區,以及靠它運作的兩支 cron。

分區換掉的是**保留機制的形狀**,不是有無:以前是 `DELETE ... WHERE created_at <
cutoff`(WAL 暴衝 + 死列等 autovacuum 回收),現在是 DROP 整個子表。所以這裡要證明
的三件事是:列會路由到正確的分區、過期的分區真的被 DROP、還在保留期內的一列都
不能少。
"""
from datetime import datetime, timedelta, timezone

import pytest
import pytest_asyncio
from sqlalchemy import func, select, text

from app.core.config import get_settings
from app.models.audit_log import DEFAULT_PARTITION, AuditLog, partition_name
from app.worker import (
    AUDIT_PARTITION_LOOKAHEAD_MONTHS,
    ensure_audit_log_partitions,
    purge_old_audit_logs,
)

pytestmark = pytest.mark.asyncio


def _months_back(anchor: datetime, count: int) -> tuple[int, int]:
    year, month = divmod(anchor.month - 1 - count, 12)
    return anchor.year + year, month + 1


async def _partitions(db) -> set[str]:
    rows = await db.scalars(
        text(
            "SELECT c.relname FROM pg_inherits i JOIN pg_class c ON c.oid = i.inhrelid "
            "WHERE i.inhparent = 'audit_logs'::regclass"
        )
    )
    return set(rows.all())


async def _create_partition(db, year: int, month: int) -> str:
    name = partition_name(year, month)
    start = datetime(year, month, 1, tzinfo=timezone.utc)
    end = (
        datetime(year + 1, 1, 1, tzinfo=timezone.utc)
        if month == 12
        else datetime(year, month + 1, 1, tzinfo=timezone.utc)
    )
    await db.execute(
        text(
            f"CREATE TABLE IF NOT EXISTS {name} PARTITION OF audit_logs "
            f"FOR VALUES FROM ('{start.date()}') TO ('{end.date()}')"
        )
    )
    await db.commit()
    return name


@pytest_asyncio.fixture
async def clean_partitions(db):
    """每個測試自己建需要的分區,跑完拆乾淨 —— 分區是 DDL,conftest 的清表碰不到。"""
    before = await _partitions(db)
    yield
    for name in (await _partitions(db)) - before:
        await db.execute(text(f"DROP TABLE IF EXISTS {name}"))
    await db.commit()


async def _emit(db, *, days_ago: float, event_type: str = "auth.login_success") -> None:
    db.add(
        AuditLog(
            event_type=event_type,
            success=True,
            created_at=datetime.now(timezone.utc) - timedelta(days=days_ago),
        )
    )
    await db.commit()


# ─ 路由

async def test_rows_land_in_the_partition_for_their_month(db, clean_partitions) -> None:
    now = datetime.now(timezone.utc)
    await _create_partition(db, now.year, now.month)
    await _emit(db, days_ago=0)

    landed = await db.scalar(
        text("SELECT tableoid::regclass::text FROM audit_logs LIMIT 1")
    )
    assert landed == partition_name(now.year, now.month)


async def test_a_month_without_a_partition_falls_into_default(db) -> None:
    """DEFAULT 不是備援方案,是**告警** —— 有東西就代表 ensure_audit_log_partitions
    漏建了。但它必須存在:少了它,那一筆 INSERT 會直接失敗,而稽核事件不該因為
    運維疏失就消失。"""
    # 往回 60 個月,那個月份不可能有人建過分區。
    year, month = _months_back(datetime.now(timezone.utc), 60)
    stale = datetime(year, month, 15, tzinfo=timezone.utc)
    db.add(AuditLog(event_type="auth.login_success", success=True, created_at=stale))
    await db.commit()

    landed = await db.scalar(
        text(
            "SELECT tableoid::regclass::text FROM audit_logs "
            "WHERE created_at = :ts"
        ),
        {"ts": stale},
    )
    assert landed == DEFAULT_PARTITION


# ─ 建分區的 cron

async def test_ensure_creates_this_month_and_the_lookahead(db, clean_partitions) -> None:
    now = datetime.now(timezone.utc)
    await ensure_audit_log_partitions({})

    existing = await _partitions(db)
    for offset in range(AUDIT_PARTITION_LOOKAHEAD_MONTHS):
        year, month = divmod(now.month - 1 + offset, 12)
        assert partition_name(now.year + year, month + 1) in existing


async def test_ensure_is_idempotent(db, clean_partitions) -> None:
    """每天都會跑,所以第二次一定要是 no-op 而不是錯誤。"""
    await ensure_audit_log_partitions({})
    first = await _partitions(db)
    await ensure_audit_log_partitions({})
    assert await _partitions(db) == first


# ─ purge

async def test_purge_drops_partitions_entirely_past_retention(db, clean_partitions) -> None:
    retention = get_settings().AUDIT_LOG_RETENTION_DAYS
    now = datetime.now(timezone.utc)
    # 往回夠多個月,確保整段都在保留窗之外。
    old_year, old_month = _months_back(now, retention // 30 + 3)
    old_name = await _create_partition(db, old_year, old_month)
    await _create_partition(db, now.year, now.month)

    db.add(
        AuditLog(
            event_type="auth.login_success", success=True,
            created_at=datetime(old_year, old_month, 15, tzinfo=timezone.utc),
        )
    )
    await _emit(db, days_ago=0)

    await purge_old_audit_logs({})

    assert old_name not in await _partitions(db)
    assert partition_name(now.year, now.month) in await _partitions(db)
    assert await db.scalar(select(func.count()).select_from(AuditLog)) == 1


async def test_purge_keeps_the_month_straddling_the_cutoff(db, clean_partitions) -> None:
    """跨在保留邊界上的那個月一列都不動。

    少留幾天資料不算問題,提早刪掉還在保留期內的稽核紀錄才是 —— 而 DROP 是整段
    一起走的,沒有「只刪一半」這個選項。
    """
    retention = get_settings().AUDIT_LOG_RETENTION_DAYS
    now = datetime.now(timezone.utc)
    cutoff_month = (now - timedelta(days=retention)).replace(tzinfo=timezone.utc)
    name = await _create_partition(db, cutoff_month.year, cutoff_month.month)

    db.add(
        AuditLog(
            event_type="auth.login_success", success=True,
            # 這一筆已經過期,但它所在的分區還有一部分在保留窗內。
            created_at=cutoff_month - timedelta(days=1),
        )
    )
    await db.commit()

    await purge_old_audit_logs({})
    assert name in await _partitions(db)


async def test_purge_never_drops_the_default_partition(db, clean_partitions) -> None:
    """DEFAULT 是「沒有對應月份」那些列的唯一去處。DROP 掉它之後,任何一筆落在
    未建分區月份的 INSERT 都會直接失敗。"""
    year, month = _months_back(datetime.now(timezone.utc), 60)
    db.add(
        AuditLog(
            event_type="auth.login_success", success=True,
            created_at=datetime(year, month, 15, tzinfo=timezone.utc),
        )
    )
    await db.commit()

    await purge_old_audit_logs({})

    assert DEFAULT_PARTITION in await _partitions(db)
    # 但裡面過期的列還是要被清掉 —— DEFAULT 不能 DROP,不代表它可以無限長大。
    assert await db.scalar(select(func.count()).select_from(AuditLog)) == 0
