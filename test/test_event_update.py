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


async def test_queue_window_is_validated_against_the_merged_result(client, admin, event):
    """等候室的兩個時間欄位先前完全沒驗 —— PATCH 改得動,卻沒有任何規則看著。

    倒過來的窗會讓 waiting_room.window() 算出負長度的登記期:抽籤永遠不開,
    而且沒有任何錯誤 —— 只有「為什麼沒人被放進來」。
    """
    opens = event.sale_starts_at
    resp = await client.patch(
        f"/v1/events/{event.id}",
        json={
            "version": 1,
            "queue_opens_at": opens.isoformat(),
            "queue_closes_at": (opens - timedelta(hours=1)).isoformat(),
        },
        headers=admin,
    )
    assert resp.status_code == 422
    assert "queue_opens_at" in resp.json()["reason"]


async def test_setting_one_queue_bound_is_allowed_when_the_effective_window_holds(
    client, admin, event
):
    """只設一邊是合法的 —— 另一邊 NULL 代表「用 sale_starts_at 推導的預設」,
    只要組出來的實效窗仍然是正的。"""
    opens = event.sale_starts_at - timedelta(hours=2)   # 遠早於預設 closes(sale-30s)
    resp = await client.patch(
        f"/v1/events/{event.id}",
        json={"version": 1, "queue_opens_at": opens.isoformat()},
        headers=admin,
    )
    assert resp.status_code == 200


async def test_one_explicit_bound_that_inverts_the_effective_window_is_rejected(
    client, admin, event
):
    """兩欄各自合法、組起來卻倒過來的窗:opens 顯式設在 sale_starts_at,closes 留
    NULL(執行期推導成 sale − 30s)→ 實效窗長度為負。

    DB 的 ck_events_queue_window 看不到這種(它只在兩欄都非 NULL 時有先後可言),
    先前的應用層驗證也只比對兩個顯式值 —— 這正是 test_event_constraints 那條
    half-open 測試裡點名「只能在應用層補」的缺口。倒過來的窗沒有任何錯誤訊號,
    只有「為什麼沒人被放進來」。
    """
    resp = await client.patch(
        f"/v1/events/{event.id}",
        json={"version": 1, "queue_opens_at": event.sale_starts_at.isoformat()},
        headers=admin,
    )
    assert resp.status_code == 422
    assert "queue_opens_at" in resp.json()["reason"]


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
        total_seats=34, price_cents=None, status=EventStatus.DRAFT,
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

async def test_a_price_change_invalidates_the_cached_meta(client, admin, event, redis):
    """下單讀的是 EventMeta 快取,不是 events 表。不清的話最多 60 秒內還按舊價賣。"""
    event_id = event.id
    before = await get_event_meta(redis, event_id=event_id)
    assert before.price_cents == 1500            # 先把快取灌熱

    resp = await client.patch(
        f"/v1/events/{event_id}", json={"version": 1, "price_cents": 2000}, headers=admin
    )
    assert resp.status_code == 200

    # 重算開自己的 session,不會命中任何測試 session 的 identity map —— 所以這裡
    # 讀到 2000 就真的證明了「Redis 被清掉、DB 被回讀」,不需要 expire_all。
    after = await get_event_meta(redis, event_id=event_id)
    assert after.price_cents == 2000


async def test_moving_the_queue_window_after_publish_moves_admission(client, admin, db):
    """schema 審查第 24 條:publish 之後 PATCH 開賣/登記時間,等候室的放行時刻要跟著動。

    以前放行時刻是 publish 當下 SET 進 Redis 的快照,PATCH 只改 DB 與登記端點 ——
    管理員把開賣延後,等候室照舊時間放人,而且沒有任何錯誤。現在它從 event meta
    推導,PATCH 既有的 invalidate_event_meta 就是失效路徑。
    """
    now = datetime.now(timezone.utc)
    event = Event(
        name="延後開賣", venue="Test Arena",
        starts_at=now + timedelta(days=30), ends_at=now + timedelta(days=30, hours=3),
        sale_starts_at=now + timedelta(minutes=5), sale_ends_at=now + timedelta(days=1),
        total_seats=100, price_cents=1500, status=EventStatus.DRAFT,
    )
    db.add(event)
    await db.commit()
    event_id = event.id
    assert (await client.post(f"/v1/events/{event_id}/publish", headers=admin)).status_code == 200

    # 登記窗現在是開的(預設 sale−10m … sale−30s):排進去,放行還沒開始。
    joined = (await client.post(f"/v1/events/{event_id}/queue", headers=admin)).json()
    assert joined["admitted"] is False

    # 把登記窗的關閉時間搬到 10 秒前 → 放行已開始 10 秒,rank 0 要在裡面。
    version = (await client.get(f"/v1/events/{event_id}")).json()["version"]
    resp = await client.patch(
        f"/v1/events/{event_id}",
        json={"version": version, "queue_closes_at": (now - timedelta(seconds=10)).isoformat()},
        headers=admin,
    )
    assert resp.status_code == 200
    status = (await client.get(f"/v1/events/{event_id}/queue/status", headers=admin)).json()
    assert status["admitted"] is True

    # 反方向也要跟:搬回未來,放行就該停 —— 位置保留(people_ahead 0),不是被踢掉。
    resp = await client.patch(
        f"/v1/events/{event_id}",
        json={"version": version + 1, "queue_closes_at": (now + timedelta(minutes=4)).isoformat()},
        headers=admin,
    )
    assert resp.status_code == 200
    status = (await client.get(f"/v1/events/{event_id}/queue/status", headers=admin)).json()
    assert (status["admitted"], status["people_ahead"]) == (False, 0)


async def test_a_window_change_pokes_the_live_streams(client, admin, event, monkeypatch):
    """SSE 連線算好「睡到放行那一刻」就睡了;放行時刻被 PATCH 搬走時要叫醒它重算,
    不然最壞要等 20 秒心跳。跟放行無關的欄位(改名)不 poke。"""
    pokes: list[int] = []

    async def spy(_redis, event_id):
        pokes.append(event_id)

    monkeypatch.setattr("app.api.v1.events.publish_event_poke", spy)

    resp = await client.patch(
        f"/v1/events/{event.id}", json={"version": 1, "name": "跟放行無關"}, headers=admin
    )
    assert resp.status_code == 200 and pokes == []

    later = event.sale_starts_at + timedelta(hours=1)
    resp = await client.patch(
        f"/v1/events/{event.id}",
        json={"version": 2, "sale_starts_at": later.isoformat()},
        headers=admin,
    )
    assert resp.status_code == 200 and pokes == [event.id]


async def test_a_naive_datetime_is_rejected_with_422_not_500(client, admin, event):
    """schema 層要求 AwareDatetime:無時區的 ISO 字串直接 422。

    這條擋的是一個真實的回歸路徑:DB 側全是 timestamptz(aware),而實效窗驗證
    會拿 payload 值跟 DB 推導值比較 —— naive 混進去是 TypeError,以 500 浮出,
    錯誤訊息跟「你少給了時區」毫無關係。HTML 的 datetime-local 輸出的正是這種
    無 offset 格式,管理員第一次從表單設時間就會踩中。
    """
    resp = await client.patch(
        f"/v1/events/{event.id}",
        json={"version": 1, "queue_opens_at": "2026-09-01T10:00:00"},   # 無時區
        headers=admin,
    )
    assert resp.status_code == 422
    assert "timezone" in str(resp.json()).lower()
