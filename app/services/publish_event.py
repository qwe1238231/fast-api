from redis.asyncio import Redis as RedisClient
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.exceptions import EventError
from app.models.event import Event, EventStatus
from app.services.inventory import set_initial_stock
from app.services.event_cache import invalidate_event_meta

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
    await invalidate_event_meta(redis, event_id=event.id)
    return event
