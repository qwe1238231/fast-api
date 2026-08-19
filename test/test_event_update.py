"""PATCH /v1/events/{id} —— 後台編輯與它的樂觀鎖。

單元層的兩道關卡在 test_optimistic_lock.py;這裡驗的是端點把它們接對了,以及
PATCH 特有的兩件事:部分更新的驗證要看合併後的值,還有 commit 之後要清快取。
"""
from datetime import datetime, timedelta, timezone

import pytest
import pytest_asyncio
from sqlalchemy import select

from app.models.event import Event, EventStatus
from app.models.seating import Venue
from app.models.user import User
from app.services.event_cache import get_event_meta

pytestmark = pytest.mark.asyncio


@pytest_asyncio.fixture
async def admin(client, db):
    await client.post("/v1/users/", json={"username": "boss", "password": "secret123"})
    user = await db.scalar(select(User).where(User.username == "boss"))
    user.is_admin = True
    await db.commit()
    token = await client.post(
        "/v1/auth/token", data={"username": "boss", "password": "secret123"}
    )
    return {"Authorization": f"Bearer {token.json()['access_token']}"}


@pytest_asyncio.fixture
async def event(db) -> Event:
    now = datetime.now(timezone.utc)
    event = Event(
        name="原始名稱", venue="Test Arena",
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


# ---- 快樂路徑與版本的往返 ----

async def test_get_exposes_the_version_so_the_client_can_send_it_back(client, event):
    """不吐 version,前端就無從帶回,整條樂觀鎖形同虛設。"""
    body = (await client.get(f"/v1/events/{event.id}")).json()
    assert body["version"] == 1


async def test_update_applies_the_change_and_bumps_the_version(client, admin, event, db):
    resp = await client.patch(
        f"/v1/events/{event.id}",
        json={"version": 1, "name": "改過的名稱", "price_cents": 2000},
        headers=admin,
    )
    assert resp.status_code == 200
    assert resp.json()["version"] == 2          # 回應要帶新版本,下一次編輯才有得送

    await db.refresh(event)
    assert (event.name, event.price_cents) == ("改過的名稱", 2000)


async def test_a_noop_patch_does_not_burn_a_version(client, admin, event):
    """只送 version、或送了跟現值相同的內容 —— 不該讓別人手上的版本失效。"""
    resp = await client.patch(
        f"/v1/events/{event.id}",
        json={"version": 1, "name": "原始名稱"},
        headers=admin,
    )
    assert resp.status_code == 200
    assert resp.json()["version"] == 1


# ---- 樂觀鎖 ----

async def test_a_stale_version_is_rejected_and_changes_nothing(client, admin, event, db):
    """兩個管理員各自開著 v1 的編輯頁,後按儲存的那個必須被擋下。"""
    first = await client.patch(
        f"/v1/events/{event.id}", json={"version": 1, "price_cents": 9999}, headers=admin
    )
    assert first.status_code == 200

    second = await client.patch(
        f"/v1/events/{event.id}", json={"version": 1, "name": "B 的改名"}, headers=admin
    )
    assert second.status_code == 409
    body = second.json()
    assert (body["expected_version"], body["current_version"]) == (1, 2)
    assert body["resource"] == "event"

    await db.refresh(event)
    assert event.price_cents == 9999            # 先寫的還在
    assert event.name == "原始名稱"              # 後寫的沒蓋掉任何東西


async def test_version_is_required(client, admin, event):
    """漏傳 version 必須是 422,不能默默當成「不檢查」。"""
    resp = await client.patch(
        f"/v1/events/{event.id}", json={"name": "沒帶版本"}, headers=admin
    )
    assert resp.status_code == 422


# ---- PATCH 特有的驗證 ----

async def test_an_unknown_field_is_rejected_not_ignored(client, admin, event):
    """欄位名打錯在寬鬆模式下會回 200 卻什麼都沒改 —— 最難查的一種 bug。"""
    resp = await client.patch(
        f"/v1/events/{event.id}",
        json={"version": 1, "pirce_cents": 2000},      # 故意拼錯
        headers=admin,
    )
    assert resp.status_code == 422


async def test_window_is_validated_against_the_merged_result(client, admin, event):
    """只送一個 sale_ends_at:單看 payload 永遠合法,合併後卻早於現有的
    sale_starts_at。POST 不會遇到這個坑,PATCH 會。"""
    too_early = event.sale_starts_at - timedelta(hours=1)
    resp = await client.patch(
        f"/v1/events/{event.id}",
        json={"version": 1, "sale_ends_at": too_early.isoformat()},
        headers=admin,
    )
    assert resp.status_code == 422
    assert "sale_starts_at" in resp.json()["reason"]


async def test_total_seats_is_not_editable(client, admin, event):
    """庫存上限不能靠一次 UPDATE 改 —— Redis 那份已經照舊值初始化了。"""
    resp = await client.patch(
        f"/v1/events/{event.id}", json={"version": 1, "total_seats": 500}, headers=admin
    )
    assert resp.status_code == 422


async def test_seated_events_reject_the_single_price_field(client, admin, db):
    """座位場次的票價按區設定,price_cents 對它沒有意義(建立時被塞成 0)。"""
    venue = Venue(name="Seated Arena")
    db.add(venue)
    await db.flush()
    now = datetime.now(timezone.utc)
    event = Event(
        name="座位場次", venue="Seated Arena", venue_id=venue.id,
        starts_at=now + timedelta(days=30), ends_at=now + timedelta(days=30, hours=3),
        sale_starts_at=now + timedelta(days=1), sale_ends_at=now + timedelta(days=2),
        total_seats=34, price_cents=0, status=EventStatus.DRAFT,
    )
    db.add(event)
    await db.commit()

    resp = await client.patch(
        f"/v1/events/{event.id}", json={"version": 1, "price_cents": 2000}, headers=admin
    )
    assert resp.status_code == 422


async def test_a_cancelled_event_cannot_be_edited(client, admin, event, db):
    event.status = EventStatus.CANCELLED
    await db.commit()

    resp = await client.patch(
        f"/v1/events/{event.id}", json={"version": 2, "name": "改不動"}, headers=admin
    )
    assert resp.status_code == 409


# ---- 權限 ----

async def test_a_normal_user_cannot_edit(client, event):
    await client.post("/v1/users/", json={"username": "nobody", "password": "secret123"})
    token = (await client.post(
        "/v1/auth/token", data={"username": "nobody", "password": "secret123"}
    )).json()["access_token"]

    resp = await client.patch(
        f"/v1/events/{event.id}",
        json={"version": 1, "price_cents": 1},
        headers={"Authorization": f"Bearer {token}"},
    )
    assert resp.status_code == 403


# ---- 快取 ----

async def test_a_price_change_invalidates_the_cached_meta(client, admin, event, db, redis):
    """下單讀的是 EventMeta 快取,不是 events 表。不清的話最多 60 秒內還按舊價賣。"""
    event_id = event.id                          # expire_all 之後再讀屬性會是一次 lazy load
    before = await get_event_meta(redis, db, event_id=event_id)
    assert before.price_cents == 1500            # 先把快取灌熱

    resp = await client.patch(
        f"/v1/events/{event_id}", json={"version": 1, "price_cents": 2000}, headers=admin
    )
    assert resp.status_code == 200

    # 快取 miss 之後 get_event_meta 會回頭讀 DB,而這個 session 的 identity map 裡
    # 還躺著改動前的 Event(expire_on_commit=False,且改動是端點那個 session 做的)。
    # 不 expire 的話這裡讀到的是記憶體裡的舊物件,測到的就不是 Redis 有沒有被清。
    db.expire_all()

    after = await get_event_meta(redis, db, event_id=event_id)
    assert after.price_cents == 2000
