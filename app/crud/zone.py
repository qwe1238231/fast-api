from sqlalchemy.ext.asyncio import AsyncSession

from app.models.seating import Zone


async def get_zone(db: AsyncSession, *, zone_id: int) -> Zone | None:
    """Look up a zone by its primary key."""
    return await db.get(Zone, zone_id)
