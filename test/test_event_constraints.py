"""events 的時間欄位不變量,兩層各測一次。

events 早就有 `total_seats > 0` 與 `price_cents >= 0` 的 CHECK,理由是「管理員
typo 會毒害庫存邏輯」。四個時間欄位是同一類問題卻長期沒防 —— 而它的故障更安靜:
sale_ends_at 早於 sale_starts_at 的場次不會噴任何錯,只是永遠賣不出一張票。

兩層的分工不是「保險」,是**錯誤碼**:
  Pydantic 層  → 422,管理員看得懂哪個欄位錯了
  DB 的 CHECK  → 擋繞過 schema 的寫入(手動 SQL、migration、未來的匯入腳本)
"""
from datetime import datetime, timedelta, timezone

import pytest
from sqlalchemy.exc import IntegrityError

from app.models.event import Event, EventStatus
from app.models.user import User
from app.core.security import get_password_hash

pytestmark = pytest.mark.asyncio


def _event(**overrides) -> Event:
    now = datetime.now(timezone.utc)
    fields = {
        "name": "Concert",
        "venue": "Arena",
        "starts_at": now + timedelta(days=30),
        "ends_at": now + timedelta(days=30, hours=3),
        "sale_starts_at": now + timedelta(days=1),
        "sale_ends_at": now + timedelta(days=2),
        "total_seats": 100,
        "price_cents": 1500,
        "status": EventStatus.DRAFT,
    }
    return Event(**{**fields, **overrides})


# ─ DB 層:CHECK 擋得住繞過 schema 的寫入

@pytest.mark.parametrize(
    ("overrides", "constraint"),
    [
        # 演出時間倒過來
        (
            lambda now: {"starts_at": now + timedelta(days=2), "ends_at": now + timedelta(days=1)},
            "ck_events_show_window",
        ),
        # 開演與結束同一秒 —— 零長度的演出也不合法,所以是 < 不是 <=
        (
            lambda now: {"starts_at": now + timedelta(days=1), "ends_at": now + timedelta(days=1)},
            "ck_events_show_window",
        ),
        # 售票窗倒過來:最安靜的那種故障
        (
            lambda now: {"sale_starts_at": now + timedelta(days=2), "sale_ends_at": now + timedelta(days=1)},
            "ck_events_sale_window",
        ),
        # 等候室的窗倒過來
        (
            lambda now: {"queue_opens_at": now + timedelta(hours=2), "queue_closes_at": now + timedelta(hours=1)},
            "ck_events_queue_window",
        ),
    ],
)
async def test_db_rejects_inverted_windows(db, overrides, constraint) -> None:
    now = datetime.now(timezone.utc)
    db.add(_event(**overrides(now)))
    with pytest.raises(IntegrityError) as excinfo:
        await db.flush()
    assert constraint in str(excinfo.value)


@pytest.mark.parametrize("field", ["queue_opens_at", "queue_closes_at"])
async def test_db_allows_a_half_open_queue_window(db, field) -> None:
    """只設一邊必須通過 —— NULL 的那一邊代表「用 sale_starts_at 推導預設值」。

    這是 ck_events_queue_window 必須帶 NULL 分支的原因。順帶標出這道網補不到的
    地方:waiting_room.window() 的兩個 fallback 是**各自獨立**計算的,所以
    「opens 設在 sale_starts_at 之後、closes 留 NULL」照樣能組出倒過來的有效窗,
    而兩個欄位各自都合法。那個缺口只能在應用層補。
    """
    now = datetime.now(timezone.utc)
    event = _event(**{field: now + timedelta(hours=1)})
    db.add(event)
    await db.commit()
    assert event.id is not None


async def test_db_allows_a_sane_event(db) -> None:
    """負向測試要有對照組,否則「全部都擋」也會是綠的。"""
    event = _event()
    db.add(event)
    await db.commit()
    assert event.id is not None


# ─ API 層:同一組規則要先在這裡變成 422,不是讓 CHECK 變成 500

async def test_post_rejects_inverted_windows_with_422(client, db) -> None:
    """專案沒有全域的 IntegrityError handler —— 少了 Pydantic 這一層,倒過來的
    日期會一路走到 INSERT,管理員收到 500 而不是「哪個欄位錯了」。"""
    db.add(User(username="admin", hashed_password=get_password_hash("secret123"), is_admin=True))
    await db.commit()
    token = await client.post(
        "/v1/auth/token", data={"username": "admin", "password": "secret123"}
    )
    headers = {"Authorization": f"Bearer {token.json()['access_token']}"}

    payload = {
        "name": "Concert",
        "venue": "Arena",
        "starts_at": "2026-12-01T22:00:00+00:00",
        "ends_at": "2026-12-01T19:00:00+00:00",       # 早於 starts_at
        "sale_starts_at": "2026-06-01T00:00:00+00:00",
        "sale_ends_at": "2026-11-30T23:59:00+00:00",
        "total_seats": 100,
        "price_cents": 1500,
    }
    resp = await client.post("/v1/events/", json=payload, headers=headers)
    assert resp.status_code == 422
    assert "ends_at" in str(resp.json())
