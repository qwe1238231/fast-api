"""樂觀鎖的兩道關卡(app/db/optimistic.py)與 Event 的 version 欄位。

刻意用**兩個獨立的 AsyncSession** 來模擬兩個管理員 —— 同一個 session 讀兩次會拿到
identity map 裡的同一個物件,根本製造不出「各自持有一份舊值」的情境,那樣的測試會
全綠但什麼都沒驗到。
"""
from datetime import datetime, timedelta, timezone

import pytest
from sqlalchemy import update

from app.core.exceptions import ConcurrentModification
from app.db.optimistic import require_version, stale_data_as_conflict
from app.db.session import AsyncSessionLocal
from app.models.event import Event, EventStatus


async def _make_event(db) -> Event:
    now = datetime.now(timezone.utc)
    event = Event(
        name="Original Name", venue="Test Arena",
        starts_at=now + timedelta(days=30),
        ends_at=now + timedelta(days=30, hours=3),
        sale_starts_at=now + timedelta(days=1),
        sale_ends_at=now + timedelta(days=2),
        total_seats=100, price_cents=1500,
        status=EventStatus.DRAFT,
    )
    db.add(event)
    await db.commit()
    return event


# ---- 版本欄位本身 ----

@pytest.mark.asyncio
async def test_version_starts_at_one_and_bumps_on_every_flush(db):
    """值由 SQLAlchemy 管,所以 model 上沒寫 default 也該是 1。"""
    event = await _make_event(db)
    assert event.version == 1

    event.price_cents = 2000
    await db.commit()
    assert event.version == 2

    event.name = "Renamed"
    await db.commit()
    assert event.version == 3


# ---- 關卡 1:客戶端帶回的版本 ----

@pytest.mark.asyncio
async def test_require_version_rejects_a_stale_client_version(db):
    """表單開太久:前端拿著 v1,但資料庫已經是 v2。"""
    event = await _make_event(db)
    event.price_cents = 2000
    await db.commit()                     # 別人先改了 → v2

    with pytest.raises(ConcurrentModification) as exc_info:
        require_version(event, expected=1, resource="event", resource_id=event.id)

    exc = exc_info.value
    assert (exc.expected_version, exc.current_version) == (1, 2)
    assert exc.resource_id == event.id


@pytest.mark.asyncio
async def test_require_version_passes_on_the_current_version(db):
    event = await _make_event(db)
    require_version(event, expected=1, resource="event", resource_id=event.id)  # 不該丟


# ---- 關卡 2:flush 撞上 ----

@pytest.mark.asyncio
async def test_second_writer_loses_and_the_first_write_survives(db):
    """兩個管理員各自讀到 v1,一個改票價、一個改名稱 —— 後寫的那個必須被擋下。

    沒有樂觀鎖時,第二次 commit 會靜默成功並把票價蓋回 1500(lost update);
    這個測試就是在證明那條路已經封死。
    """
    event = await _make_event(db)
    event_id = event.id

    async with AsyncSessionLocal() as db_a, AsyncSessionLocal() as db_b:
        event_a = await db_a.get(Event, event_id)
        event_b = await db_b.get(Event, event_id)   # 兩份各自持有 version=1
        assert event_a.version == event_b.version == 1

        event_a.price_cents = 9999
        await db_a.commit()                          # A 先到 → v2

        event_b.name = "B's Rename"
        with pytest.raises(ConcurrentModification) as exc_info:
            async with stale_data_as_conflict(
                db_b, resource="event", resource_id=event_id, expected_version=1
            ):
                await db_b.commit()                  # B 的 UPDATE ... WHERE version=1 撲空

        # flush 那道只知道 rowcount=0,答不出誰贏了 —— 前端一律重新載入。
        assert exc_info.value.current_version is None

    async with AsyncSessionLocal() as db_check:
        fresh = await db_check.get(Event, event_id)
        assert fresh.price_cents == 9999             # A 的改動還在
        assert fresh.name == "Original Name"         # B 的沒有蓋掉任何東西
        assert fresh.version == 2


@pytest.mark.asyncio
async def test_conflict_leaves_the_session_usable(db):
    """flush 失敗後 session 會停在 pending-rollback;沒有 rollback 的話,後續任何
    一次使用都會炸在一個跟真正原因無關的例外上(而請求還沒結束)。"""
    event = await _make_event(db)
    event_id = event.id

    async with AsyncSessionLocal() as db_b:
        event_b = await db_b.get(Event, event_id)

        async with AsyncSessionLocal() as db_a:
            event_a = await db_a.get(Event, event_id)
            event_a.price_cents = 9999
            await db_a.commit()

        event_b.name = "B's Rename"
        with pytest.raises(ConcurrentModification):
            async with stale_data_as_conflict(db_b, resource="event", resource_id=event_id):
                await db_b.commit()

        # 這一句在沒 rollback 的 session 上會丟 PendingRollbackError。
        assert (await db_b.get(Event, event_id)).price_cents == 9999


# ---- 已知的破口:Core update 繞過版本 ----

@pytest.mark.asyncio
async def test_core_update_bypasses_the_version_check(db):
    """這個測試斷言的是一個**限制**,不是一個保證。

    `version_id_col` 是 ORM 在 flush 時維護的,`update(Event).where(...)` 這種 Core
    statement 既不遞增也不比對版本。哪天有人為了效能把後台改成 bulk update,鎖會
    靜默失效 —— 這個測試會跟著紅,提醒他這件事,而不是等資料被蓋掉才發現。
    """
    event = await _make_event(db)

    await db.execute(
        update(Event).where(Event.id == event.id).values(price_cents=4242)
    )
    await db.commit()
    await db.refresh(event)

    assert event.price_cents == 4242
    assert event.version == 1        # 沒有前進 —— 別人手上的 v1 依然會被放行
