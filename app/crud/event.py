from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.event import Event, EventStatus

# create_event 搬去 app/services/event_admin.py:座位場次需要驗證場館與 zone_prices、
# 並從座位圖推導 total_seats,那些是領域規則而不是單純的 DB 寫入。

async def get_event(db: AsyncSession, event_id:int) -> Event | None:
    return await db.get(Event, event_id)

async def list_published_events(db: AsyncSession, *, offset: int = 0, limit: int = 50) -> list[Event]:
    """Public listing: only published events, soonest sale first."""
    stmt = (
        select(Event)
        .where(Event.status == EventStatus.PUBLISHED)
        .order_by(Event.sale_starts_at.asc())
        .offset(offset)
        .limit(limit)
    )
    result = await db.execute(stmt)
    return list(result.scalars().all())