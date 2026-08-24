from redis.asyncio import Redis as RedisClient
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.exceptions import EventError, SeatMapMismatch, ZonePricesIncomplete
from app.models.event import Event, EventStatus
from app.models.seating import EventZonePrice, SeatBlock, Zone
from app.services.inventory import set_initial_stock
from app.services.event_cache import invalidate_event_meta
from app.services.seat_runs import rebuild_zone_runs
from app.services.waiting_room import setup as setup_waiting_room

async def publish_event(
        db: AsyncSession,
        redis: RedisClient,
        event: Event
) -> Event:
    """draft → published,並把庫存灌進 Redis。Caller commits."""
    if event.status != EventStatus.DRAFT:
        raise EventError(
            f"Cannot publish event with status {event.status.value}"
        )
    if event.venue_id is not None:
        # total_seats 對座位場次是衍生值,但沒有資料庫約束能表達跨表關係 —— 填錯
        # 會讓 detect_inventory_drift 永久誤報(它用 total_seats − SUM(quantity)
        # 算期望值),漂移偵測就變成狼來了。在這裡擋,而且擋在改狀態之前。
        capacity = await db.scalar(
            select(func.coalesce(func.sum(SeatBlock.capacity), 0))
            .join(Zone, Zone.id == SeatBlock.zone_id)
            .where(Zone.venue_id == event.venue_id)
        )
        if capacity != event.total_seats:
            raise SeatMapMismatch(event.id, event.total_seats, capacity)

        # 每個 zone 都必須有票價。少一個的話它的容量算進 total_seats(上面那條
        # 檢查強制的)但永遠賣不掉 —— event:available 的下限就永遠 > 0,等候室的
        # sold_out 永不觸發,而漂移偵測因為內部自洽也不會叫。
        unpriced = list(
            (
                await db.scalars(
                    select(Zone.id)
                    .outerjoin(
                        EventZonePrice,
                        (EventZonePrice.zone_id == Zone.id)
                        & (EventZonePrice.event_id == event.id),
                    )
                    .where(
                        Zone.venue_id == event.venue_id,
                        EventZonePrice.price_cents.is_(None),
                    )
                    .order_by(Zone.id)
                )
            ).all()
        )
        if unpriced:
            raise ZonePricesIncomplete(event.id, unpriced)

    event.status = EventStatus.PUBLISHED
    await db.flush()
    await set_initial_stock(redis, event_id=event.id, total_seats=event.total_seats)
    if event.venue_id is not None:
        # 座位場次:把每個 zone 的空段結構灌進 Redis。少了這一步,配位會看到零個
        # 空段、每一筆訂單都回「配不出來」—— 而那跟「真的湊不出連號」長得一模一樣,
        # 沒有人會意識到只是忘了初始化。所以它必須跟 set_initial_stock 綁在一起。
        zone_ids = (
            await db.scalars(select(Zone.id).where(Zone.venue_id == event.venue_id))
        ).all()
        for zone_id in zone_ids:
            # force:場次此刻才要開賣,不可能有 in-flight intent 指向它。而
            # queue_depth 是**全域**的 —— 不 force 的話,別的場次有 backlog 就
            # 發佈不了新場次。
            await rebuild_zone_runs(
                db, redis, event_id=event.id, zone_id=zone_id, force=True
            )
    await setup_waiting_room(redis, event)          # fix salt + admission-start for the queue
    await invalidate_event_meta(redis, event_id=event.id)
    return event
