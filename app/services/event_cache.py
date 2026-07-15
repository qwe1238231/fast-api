"""Event meta cache — cache-aside for the per-order hot read.

下單每次都要讀 event 設定(status / 售票窗 / 價格),但這些在開賣期間是靜態的。
快取在 Redis,大幅減少打 Postgres 的次數。
"""
import json
from dataclasses import dataclass
from datetime import datetime

from redis.asyncio import Redis
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.event import Event, EventStatus

_TTL_SECONDS = 60 

@dataclass
class EventMeta:
    status: EventStatus
    sale_starts_at: datetime
    sale_ends_at: datetime
    price_cents: int

def _key(event_id: int) -> str:
    return f"event:{event_id}:meta"

async def get_event_meta(
        redis: Redis,
        db: AsyncSession,
        *,
        event_id: int,
) -> EventMeta | None:
    """Cache-aside:先讀 Redis,miss 才回 Postgres 並回填。"""
    cached = await redis.get(_key(event_id))
    if cached is not None:
        d = json.loads(cached)
        return EventMeta(
            status=EventStatus(d["status"]),
            sale_starts_at=datetime.fromisoformat(d["sale_starts_at"]),
            sale_ends_at=datetime.fromisoformat(d["sale_ends_at"]),
            price_cents=d["price_cents"],
        )
    
    event = await db.get(Event, event_id)
    if event is None:
        return None
    
    await redis.set(
        _key(event_id),
        json.dumps({
            "status": event.status.value,
            "sale_starts_at": event.sale_starts_at.isoformat(),
            "sale_ends_at": event.sale_ends_at.isoformat(),
            "price_cents": event.price_cents,
        }),
        ex=_TTL_SECONDS
    )
    return EventMeta(
        event.status, event.sale_starts_at, event.sale_ends_at, event.price_cents
    )

async def invalidate_event_meta(redis: Redis, *, event_id: int) -> None:
    """活動狀態改變時清掉快取(例如發佈)。"""
    await redis.delete(_key(event_id))