from redis.asyncio import Redis as RedisClient
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.exceptions import EventError
from app.models.event import Event, EventStatus
from app.models.seating import Zone
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
            await rebuild_zone_runs(db, redis, event_id=event.id, zone_id=zone_id)
    await setup_waiting_room(redis, event)          # fix salt + admission-start for the queue
    await invalidate_event_meta(redis, event_id=event.id)
    return event
