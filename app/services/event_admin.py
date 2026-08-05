"""建立場次 —— 兩種形狀的驗證與衍生。

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

from app.core.exceptions import VenueNotFound, ZonePricesInvalid
from app.models.event import Event
from app.models.seating import EventZonePrice, SeatBlock, Venue, Zone
from app.schemas.event import EventCreate


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
