from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.event import Event, EventStatus
from app.schemas.event import EventCreate

async def create_event(db: AsyncSession, data:EventCreate) -> Event:
    """Insert a new event (status defaults to draft). Caller commits."""
    event = Event(**data.model_dump())
    db.add(event)
    await db.flush()
    return event

async def get_event(db: AsyncSession, event_id:int) -> Event | None:
    return await db.get(Event, event_id)

async def list_published_events(db: AsyncSession, *, limit:int = 50) -> list[Event]:
    """Public listing: only published events, soonest sale first."""
    stmt = (
        select(Event)
        .where(Event.status == EventStatus.PUBLISHED)
        .order_by(Event.sale_starts_at.asc())
        .limit(limit)
    )
    result = await db.execute(stmt)
    return list(result.scalars().all())