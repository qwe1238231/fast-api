"""後台改「票種名稱」與「分區票價」—— 兩張表各自的樂觀鎖。

粒度是這個檔案的重點:版本掛在**每一列**上。改 A 區的票價不該讓拿著 B 區版本的
另一個管理員被踢出 409 —— 那是把「聚合版本」誤當成樂觀鎖時最典型的症狀。
"""
from datetime import datetime, timedelta, timezone

import pytest
import pytest_asyncio
from sqlalchemy import select

from app.models.event import Event, EventStatus
from app.models.seating import EventZonePrice, Venue, Zone
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
async def seated(db):
    """一個有兩區(搖滾 2000 / 看台 1000)的場次。回 (event, front_zone, back_zone)。"""
    venue = Venue(name="Zone Admin Arena")
    db.add(venue)
    await db.flush()
    front = Zone(venue_id=venue.id, name="搖滾區", display_order=0)
    back = Zone(venue_id=venue.id, name="看台", display_order=1)
    db.add_all([front, back])
    await db.flush()

    now = datetime.now(timezone.utc)
    event = Event(
        name="座位場次", venue=venue.name, venue_id=venue.id,
        starts_at=now + timedelta(days=30), ends_at=now + timedelta(days=30, hours=3),
        sale_starts_at=now + timedelta(days=1), sale_ends_at=now + timedelta(days=2),
        total_seats=34, price_cents=0, status=EventStatus.DRAFT,
    )
    db.add(event)
    await db.flush()
    db.add_all([
        EventZonePrice(event_id=event.id, zone_id=front.id, price_cents=2000),
        EventZonePrice(event_id=event.id, zone_id=back.id, price_cents=1000),
    ])
    await db.commit()
    return event, front, back


# ---- 分區票價 ----

async def test_batch_price_update_returns_every_row_with_its_new_version(
    client, admin, seated
):
    """只改一列,但回的是完整價格表 —— 下一次儲存需要每一列的版本。"""
    event, front, back = seated
    resp = await client.patch(
        f"/v1/events/{event.id}/zone-prices",
        json={"prices": [{"zone_id": front.id, "price_cents": 2500, "version": 1}]},
        headers=admin,
    )
    assert resp.status_code == 200

    rows = {row["zone_id"]: row for row in resp.json()}
    assert rows[front.id] == {"zone_id": front.id, "price_cents": 2500, "version": 2}
    assert rows[back.id] == {"zone_id": back.id, "price_cents": 1000, "version": 1}


async def test_editing_one_zone_does_not_invalidate_another_zones_version(
    client, admin, seated
):
    """逐列版本的重點:A 區被改過之後,拿著 B 區 v1 的人仍然存得了。

    版本若掛在 event 這個聚合上,這一段就會變成 409 —— 一個沒有任何真實衝突的
    409,而且會逼管理員重新載入整張價格表。
    """
    event, front, back = seated
    first = await client.patch(
        f"/v1/events/{event.id}/zone-prices",
        json={"prices": [{"zone_id": front.id, "price_cents": 2500, "version": 1}]},
        headers=admin,
    )
    assert first.status_code == 200

    second = await client.patch(
        f"/v1/events/{event.id}/zone-prices",
        json={"prices": [{"zone_id": back.id, "price_cents": 1200, "version": 1}]},
        headers=admin,
    )
    assert second.status_code == 200


async def test_a_stale_row_rejects_the_whole_batch(client, admin, seated, db):
    """整批同一個 transaction:一列版本不符,另一列也不准生效。

    否則管理員拿到的是一份改到一半的價格表 —— 而他只會看到一個 409。
    """
    event, front, back = seated
    event_id, front_id, back_id = event.id, front.id, back.id   # expire_all 之前先抓
    await client.patch(
        f"/v1/events/{event_id}/zone-prices",
        json={"prices": [{"zone_id": front_id, "price_cents": 2500, "version": 1}]},
        headers=admin,
    )

    resp = await client.patch(
        f"/v1/events/{event_id}/zone-prices",
        json={"prices": [
            {"zone_id": front_id, "price_cents": 3000, "version": 1},   # 過期
            {"zone_id": back_id, "price_cents": 1200, "version": 1},    # 這列本身是新的
        ]},
        headers=admin,
    )
    assert resp.status_code == 409
    assert resp.json()["resource_id"] == front_id

    db.expire_all()
    prices = {
        row.zone_id: row.price_cents
        for row in (await db.scalars(
            select(EventZonePrice).where(EventZonePrice.event_id == event_id)
        )).all()
    }
    assert prices == {front_id: 2500, back_id: 1000}   # 沒改到一半


async def test_duplicate_zone_in_one_batch_is_rejected(client, admin, seated):
    """同一區出現兩次的話「最後一筆贏」,前一筆的版本檢查等於被繞過去了。"""
    event, front, _ = seated
    resp = await client.patch(
        f"/v1/events/{event.id}/zone-prices",
        json={"prices": [
            {"zone_id": front.id, "price_cents": 2500, "version": 1},
            {"zone_id": front.id, "price_cents": 9999, "version": 1},
        ]},
        headers=admin,
    )
    assert resp.status_code == 422


async def test_a_zone_from_another_venue_is_rejected(client, admin, seated, db):
    """別場館的 zone_id 不能靠改價的端點溜進這個場次的價格表。"""
    event, _, _ = seated
    other_venue = Venue(name="別的場館")
    db.add(other_venue)
    await db.flush()
    stranger = Zone(venue_id=other_venue.id, name="搖滾區", display_order=0)
    db.add(stranger)
    await db.commit()

    resp = await client.patch(
        f"/v1/events/{event.id}/zone-prices",
        json={"prices": [{"zone_id": stranger.id, "price_cents": 1, "version": 1}]},
        headers=admin,
    )
    assert resp.status_code == 422


async def test_price_change_invalidates_the_meta_cache(client, admin, seated, db, redis):
    """下單算錢讀的是 EventMeta 的 zone_prices,不是 event_zone_prices 表。"""
    event, front, _ = seated
    event_id, front_id = event.id, front.id

    before = await get_event_meta(redis, db, event_id=event_id)
    assert before.zone_prices[front_id] == 2000        # 灌熱

    resp = await client.patch(
        f"/v1/events/{event_id}/zone-prices",
        json={"prices": [{"zone_id": front_id, "price_cents": 2500, "version": 1}]},
        headers=admin,
    )
    assert resp.status_code == 200

    db.expire_all()      # 否則 cache miss 後的 DB 回讀會命中這個 session 的舊物件
    after = await get_event_meta(redis, db, event_id=event_id)
    assert after.zone_prices[front_id] == 2500


async def test_a_normal_user_cannot_change_prices(client, seated):
    event, front, _ = seated
    await client.post("/v1/users/", json={"username": "nobody", "password": "secret123"})
    token = (await client.post(
        "/v1/auth/token", data={"username": "nobody", "password": "secret123"}
    )).json()["access_token"]

    resp = await client.patch(
        f"/v1/events/{event.id}/zone-prices",
        json={"prices": [{"zone_id": front.id, "price_cents": 1, "version": 1}]},
        headers={"Authorization": f"Bearer {token}"},
    )
    assert resp.status_code == 403


# ---- 票種名稱 ----

async def test_rename_a_zone(client, admin, seated, db):
    _, front, _ = seated
    assert (await client.get(f"/v1/zones/{front.id}", headers=admin)).json()["version"] == 1

    resp = await client.patch(
        f"/v1/zones/{front.id}", json={"version": 1, "name": "搖滾A區"}, headers=admin
    )
    assert resp.status_code == 200
    assert resp.json()["name"] == "搖滾A區"
    assert resp.json()["version"] == 2

    await db.refresh(front)
    assert front.name == "搖滾A區"


async def test_a_stale_version_cannot_overwrite_a_rename(client, admin, seated, db):
    _, front, _ = seated
    await client.patch(
        f"/v1/zones/{front.id}", json={"version": 1, "display_order": 5}, headers=admin
    )

    resp = await client.patch(
        f"/v1/zones/{front.id}", json={"version": 1, "name": "後到的改名"}, headers=admin
    )
    assert resp.status_code == 409
    assert (resp.json()["expected_version"], resp.json()["current_version"]) == (1, 2)

    await db.refresh(front)
    assert (front.name, front.display_order) == ("搖滾區", 5)


async def test_renaming_onto_a_sibling_name_is_a_409_not_a_500(client, admin, seated):
    """同場館內區名唯一(uq_zones_venue_name)。這是可預期的管理員輸入,
    不該漏成 IntegrityError。"""
    _, front, back = seated
    resp = await client.patch(
        f"/v1/zones/{front.id}", json={"version": 1, "name": back.name}, headers=admin
    )
    assert resp.status_code == 409
    assert resp.json()["name"] == back.name


async def test_the_same_name_in_another_venue_is_fine(client, admin, seated, db):
    """唯一性是 venue-scoped 的 —— 每個場館都可以有自己的「搖滾區」。"""
    _, front, _ = seated
    other = Venue(name="另一個場館")
    db.add(other)
    await db.flush()
    twin = Zone(venue_id=other.id, name="看台", display_order=0)
    db.add(twin)
    await db.commit()

    resp = await client.patch(
        f"/v1/zones/{twin.id}", json={"version": 1, "name": front.name}, headers=admin
    )
    assert resp.status_code == 200


async def test_unknown_zone_is_404(client, admin):
    resp = await client.patch(
        "/v1/zones/999999", json={"version": 1, "name": "不存在"}, headers=admin
    )
    assert resp.status_code == 404


async def test_a_normal_user_cannot_rename_a_zone(client, seated):
    _, front, _ = seated
    await client.post("/v1/users/", json={"username": "nobody", "password": "secret123"})
    token = (await client.post(
        "/v1/auth/token", data={"username": "nobody", "password": "secret123"}
    )).json()["access_token"]

    resp = await client.patch(
        f"/v1/zones/{front.id}",
        json={"version": 1, "name": "亂改"},
        headers={"Authorization": f"Bearer {token}"},
    )
    assert resp.status_code == 403
