"""後台寫入端點的稽核紀錄。

`updated_at` 只說得出「有人改過」。改票價是直接影響營收的操作,而 zone 改名會
連動該場館所有場次、訂單又沒有快照區名 —— 對這兩件事,稽核紀錄是唯一的歷史來源。

所以這裡驗的不只是「有發事件」,還有 payload 裡真的帶著 before/after 與 actor:
一條只寫「event 3 被改過」的稽核跟沒有是一樣的。
"""
import json
from datetime import datetime, timedelta, timezone

import pytest
import pytest_asyncio
from sqlalchemy import select

from app.models.event import Event, EventStatus
from app.models.seating import EventZonePrice, Venue, Zone
from app.models.user import User
from app.services.audit import AUDIT_STREAM_KEY

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
    return user.id, {"Authorization": f"Bearer {token.json()['access_token']}"}


@pytest_asyncio.fixture
async def event(db):
    now = datetime.now(timezone.utc)
    event = Event(
        name="原始名稱", venue="Audit Arena",
        starts_at=now + timedelta(days=30), ends_at=now + timedelta(days=30, hours=3),
        sale_starts_at=now + timedelta(days=1), sale_ends_at=now + timedelta(days=2),
        total_seats=100, price_cents=1500, status=EventStatus.DRAFT,
    )
    db.add(event)
    await db.commit()
    return event


async def _audit_events(redis, event_type: str) -> list[dict]:
    """讀出 stream 裡指定類型的稽核事件,payload 解回 dict。

    直接讀 Redis Stream 而不是查 audit_logs 表:emit 是同步的、落庫是 worker 批次
    做的,查表會驗到 worker 而不是端點。
    """
    entries = await redis.xrange(AUDIT_STREAM_KEY)
    return [
        {**fields, "payload": json.loads(fields["payload"])}
        for _, fields in entries
        if fields["event_type"] == event_type
    ]


async def test_event_update_records_who_changed_what(client, admin, event, redis):
    admin_id, headers = admin
    await client.patch(
        f"/v1/events/{event.id}",
        json={"version": 1, "name": "改過的名稱", "price_cents": 2000},
        headers=headers,
    )

    events = await _audit_events(redis, "event.updated")
    assert len(events) == 1
    record = events[0]
    assert record["actor_user_id"] == str(admin_id)
    assert record["target_type"] == "event"
    assert record["target_id"] == str(event.id)
    assert record["payload"]["changes"] == {
        "name": {"from": "原始名稱", "to": "改過的名稱"},
        "price_cents": {"from": 1500, "to": 2000},
    }


async def test_only_the_fields_that_actually_changed_are_recorded(
    client, admin, event, redis
):
    """送了跟現值相同的欄位不該進稽核 —— 否則每次儲存都會留下一份假的『改過』。"""
    _, headers = admin
    await client.patch(
        f"/v1/events/{event.id}",
        json={"version": 1, "name": "原始名稱", "price_cents": 2000},
        headers=headers,
    )

    changes = (await _audit_events(redis, "event.updated"))[0]["payload"]["changes"]
    assert set(changes) == {"price_cents"}


async def test_a_noop_update_records_nothing(client, admin, event, redis):
    _, headers = admin
    resp = await client.patch(
        f"/v1/events/{event.id}", json={"version": 1, "name": "原始名稱"}, headers=headers
    )
    assert resp.status_code == 200
    assert await _audit_events(redis, "event.updated") == []


async def test_a_rejected_update_records_nothing(client, admin, event, redis):
    """稽核記的是已經發生的事。被 409 擋下的那次沒有改變任何東西。"""
    _, headers = admin
    await client.patch(
        f"/v1/events/{event.id}", json={"version": 1, "price_cents": 2000}, headers=headers
    )
    stale = await client.patch(
        f"/v1/events/{event.id}", json={"version": 1, "name": "搶輸的"}, headers=headers
    )
    assert stale.status_code == 409

    events = await _audit_events(redis, "event.updated")
    assert len(events) == 1                       # 只有先成功的那一次
    assert set(events[0]["payload"]["changes"]) == {"price_cents"}


async def test_price_change_records_the_amounts(client, admin, db, redis):
    """事後查帳要的是金額本身,不是一句『價格被改過』。"""
    admin_id, headers = admin
    venue = Venue(name="Audit Seated Arena")
    db.add(venue)
    await db.flush()
    zone = Zone(venue_id=venue.id, name="搖滾區", display_order=0)
    db.add(zone)
    await db.flush()
    now = datetime.now(timezone.utc)
    event = Event(
        name="座位場次", venue=venue.name, venue_id=venue.id,
        starts_at=now + timedelta(days=30), ends_at=now + timedelta(days=30, hours=3),
        sale_starts_at=now + timedelta(days=1), sale_ends_at=now + timedelta(days=2),
        total_seats=24, price_cents=0, status=EventStatus.DRAFT,
    )
    db.add(event)
    await db.flush()
    db.add(EventZonePrice(event_id=event.id, zone_id=zone.id, price_cents=2000))
    await db.commit()

    await client.patch(
        f"/v1/events/{event.id}/zone-prices",
        json={"prices": [{"zone_id": zone.id, "price_cents": 2500, "version": 1}]},
        headers=headers,
    )

    record = (await _audit_events(redis, "event.zone_prices_updated"))[0]
    assert record["actor_user_id"] == str(admin_id)
    # key 是 str(zone_id) —— JSON 的物件鍵一定是字串,服務層就轉好了。
    assert record["payload"]["changes"] == {
        str(zone.id): {"from": 2000, "to": 2500}
    }


async def test_zone_rename_records_the_old_name(client, admin, db, redis):
    """訂單沒有快照區名,所以這條紀錄是『這張票當初叫什麼』的唯一來源。"""
    _, headers = admin
    venue = Venue(name="Rename Arena")
    db.add(venue)
    await db.flush()
    zone = Zone(venue_id=venue.id, name="搖滾區", display_order=0)
    db.add(zone)
    await db.commit()

    await client.patch(
        f"/v1/zones/{zone.id}", json={"version": 1, "name": "搖滾A區"}, headers=headers
    )

    record = (await _audit_events(redis, "zone.updated"))[0]
    assert record["target_id"] == str(zone.id)
    assert record["payload"]["venue_id"] == venue.id
    assert record["payload"]["changes"] == {
        "name": {"from": "搖滾區", "to": "搖滾A區"}
    }
