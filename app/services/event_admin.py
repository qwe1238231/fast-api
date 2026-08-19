"""建立與編輯場次 —— 兩種形狀的驗證與衍生。

無座位圖的場次是舊路徑:管理員給 `price_cents` 與 `total_seats`,照舊。

座位場次則不同:`total_seats` 是 Σ block 容量的**衍生值**。要求管理員手打再在
publish 時驗證(SeatMapMismatch),等於刻意製造一類必然會發生的設定錯誤 ——
所以這裡直接從座位圖推導。publish 的那條檢查因此退化成安全網(擋「有人事後
改了 DB」),而不是主要的守衛。

zone_prices 也在這裡驗到底:**必須恰好涵蓋該場館的每一個 zone**。
  - 少一個 → 那區的容量算進 total_seats 卻永遠賣不掉,場次永遠賣不完、等候室的
    sold_out 永不觸發,而漂移偵測因內部自洽不會叫。
  - 多一個(別場館的 zone)→ 安全問題:之後可以拿它的便宜票價下單。
兩者都在建立時擋,而不是等到 publish 才發現。
"""
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.exceptions import (
    EventCancelled, InvalidEventUpdate, VenueNotFound, ZoneNotForEvent,
    ZonePricesInvalid,
)
from app.db.optimistic import require_version
from app.models.event import Event, EventStatus
from app.models.seating import EventZonePrice, SeatBlock, Venue, Zone
from app.schemas.event import EventCreate, EventUpdate
from app.schemas.seating import ZonePricesUpdate
from app.services.audit import FieldDiff


async def create_event(db: AsyncSession, data: EventCreate) -> Event:
    """Insert a new event (status defaults to draft). Caller commits."""
    payload = data.model_dump(exclude={"zone_prices"})
    if data.venue_id is None:
        event = Event(**payload)
        db.add(event)
        await db.flush()
        return event

    if not await db.scalar(select(Venue.id).where(Venue.id == data.venue_id)):
        raise VenueNotFound(venue_id=data.venue_id)

    zone_ids = set(
        (await db.scalars(select(Zone.id).where(Zone.venue_id == data.venue_id))).all()
    )
    given = set(data.zone_prices or {})
    if given != zone_ids:
        raise ZonePricesInvalid(
            venue_id=data.venue_id,
            missing=sorted(zone_ids - given),
            unknown=sorted(given - zone_ids),
        )

    # total_seats 由座位圖推導,不接受呼叫端提供(schema 已經擋掉了)。
    payload["total_seats"] = await db.scalar(
        select(func.coalesce(func.sum(SeatBlock.capacity), 0))
        .join(Zone, Zone.id == SeatBlock.zone_id)
        .where(Zone.venue_id == data.venue_id)
    )
    payload["price_cents"] = 0        # 座位場次不用單一票價,但欄位仍 NOT NULL

    event = Event(**payload)
    db.add(event)
    await db.flush()
    db.add_all([
        EventZonePrice(event_id=event.id, zone_id=zone_id, price_cents=price)
        for zone_id, price in (data.zone_prices or {}).items()
    ])
    await db.flush()
    return event


#: 互有先後關係的四個時間欄位 —— 部分更新後要一起重驗。
_WINDOW_FIELDS = ("starts_at", "ends_at", "sale_starts_at", "sale_ends_at")



def apply_event_update(event: Event, data: EventUpdate) -> FieldDiff:
    """把後台的部分更新套到 event 上,回傳實際變動的 before/after。

    回 diff 而不是 bool:呼叫端本來就要判斷「有沒有變」(空 dict 是 falsy),而稽核
    需要的正是同一份資料。分成兩個回傳值的話,兩邊遲早會不一致 —— 而不一致的那個
    會是稽核,因為沒有人會發現它少記了一個欄位。

    沒有 I/O:版本比對與所有規則都只看手上這個物件。呼叫端負責 commit(而且要用
    stale_data_as_conflict 包住 —— 這裡的 require_version 擋的是「表單開太久」,
    擋不掉「兩個請求前後腳進來」)。

    順序是刻意的:**先比對版本、全部驗完,才動任何一個欄位**。中途才發現不合法
    的話,session 裡已經躺著一半的修改,而它會在下一次 flush 被寫進去 —— 端點就算
    正確地丟了例外也救不回來。
    """
    require_version(event, expected=data.version, resource="event", resource_id=event.id)

    if event.status is EventStatus.CANCELLED:
        raise EventCancelled(event_id=event.id)

    changes = data.model_dump(exclude_unset=True, exclude={"version"})
    if not changes:
        return {}                         # 合法的 no-op:不動版本,也不用清快取

    if "price_cents" in changes and event.venue_id is not None:
        raise InvalidEventUpdate(
            event.id,
            "座位場次的票價按區設定(EventZonePrice),不是 price_cents",
        )

    # 部分更新的驗證必須看**套用後**的值,不能只看 payload:只送一個
    # sale_ends_at 的請求,單看 payload 永遠合法,合併之後卻可能早於現有的
    # sale_starts_at。這是 PATCH 特有的坑,POST 不會遇到。
    merged = {field: changes.get(field, getattr(event, field)) for field in _WINDOW_FIELDS}
    if merged["starts_at"] >= merged["ends_at"]:
        raise InvalidEventUpdate(event.id, "starts_at 必須早於 ends_at")
    if merged["sale_starts_at"] >= merged["sale_ends_at"]:
        raise InvalidEventUpdate(event.id, "sale_starts_at 必須早於 sale_ends_at")

    diff: FieldDiff = {}
    for field, value in changes.items():
        before = getattr(event, field)
        if before != value:
            setattr(event, field, value)
            diff[field] = {"from": before, "to": value}
    return diff


async def update_zone_prices(
        db: AsyncSession,
        event: Event,
        data: ZonePricesUpdate,
) -> tuple[list[EventZonePrice], FieldDiff]:
    """整批調整分區票價。回傳 (該場次的完整價格表, 逐區的 before/after)。

    回完整的價格表而不是只回改動的那幾列:管理員的下一次儲存需要**每一列**的新
    版本,只回一部分的話沒改到的那幾列版本從哪來就得由前端自己推,而那正是版本
    不同步的開端。

    已成立的訂單不受影響 —— `orders.total_price_cents` 是下單當下的快照。這也是
    改價能這麼輕鬆的原因:它只影響「之後」的訂單。
    """
    if event.status is EventStatus.CANCELLED:
        raise EventCancelled(event_id=event.id)

    rows = {
        row.zone_id: row
        for row in (await db.scalars(
            select(EventZonePrice).where(EventZonePrice.event_id == event.id)
        )).all()
    }

    # 先驗完整批(存在性 + 版本)才動任何一列 —— 跟 apply_event_update 同一個理由:
    # 中途丟例外的話,session 裡會躺著一半的修改等著被下一次 flush 寫進去。
    for item in data.prices:
        row = rows.get(item.zone_id)
        if row is None:
            raise ZoneNotForEvent(
                event_id=event.id, zone_id=item.zone_id, reason="no price row for this event"
            )
        require_version(
            row, expected=item.version,
            resource="event_zone_price", resource_id=item.zone_id,
        )

    # diff 的 key 是 str(zone_id):它會變成 JSONB 的物件鍵,而 JSON 的鍵一定是
    # 字串。在這裡就轉好,免得之後查稽核紀錄時得記得「這裡的鍵是數字還是字串」。
    diff: FieldDiff = {}
    for item in data.prices:
        row = rows[item.zone_id]
        if row.price_cents != item.price_cents:
            diff[str(item.zone_id)] = {"from": row.price_cents, "to": item.price_cents}
            row.price_cents = item.price_cents

    return sorted(rows.values(), key=lambda row: row.zone_id), diff
