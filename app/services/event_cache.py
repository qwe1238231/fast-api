"""Event meta cache — cache-aside for the per-order hot read.

下單每次都要讀 event 設定(status / 售票窗 / 價格),但這些在開賣期間是靜態的。
快取在 Redis,大幅減少打 Postgres 的次數。

分區票價也放在這裡而不是每筆訂單查一次 DB:價格表跟其他 meta 一樣在開賣期間
靜態,而下單是最熱的路徑。附帶的好處是「這個 zone 能賣給這個場次嗎」變成一次
dict 查找 —— 見 pricing.load_zone_prices 的白名單語意。
"""
import json
from dataclasses import dataclass, field
from datetime import datetime

from redis.asyncio import Redis
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.event import Event, EventStatus
from app.services.pricing import load_zone_prices

_TTL_SECONDS = 60

@dataclass
class EventMeta:
    event_id: int
    status: EventStatus
    sale_starts_at: datetime
    sale_ends_at: datetime
    price_cents: int
    """單一票價。只在 venue_id is None(無座位圖)時使用。"""

    venue_id: int | None = None
    zone_prices: dict[int, int] = field(default_factory=dict)
    """zone_id → 單價(分)。key 存在 == 該區屬於本場館且已設價,可以賣。"""

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
            event_id=event_id,
            status=EventStatus(d["status"]),
            sale_starts_at=datetime.fromisoformat(d["sale_starts_at"]),
            sale_ends_at=datetime.fromisoformat(d["sale_ends_at"]),
            price_cents=d["price_cents"],
            venue_id=d.get("venue_id"),
            # JSON 的物件 key 一定是字串,轉回 int 否則每次查找都 miss ——
            # 那會讓每一個 zone 都變成「不可賣」,而且沒有任何錯誤訊息。
            zone_prices={int(k): v for k, v in d.get("zone_prices", {}).items()},
        )

    event = await db.get(Event, event_id)
    if event is None:
        return None

    zone_prices = await load_zone_prices(
        db, event_id=event_id, venue_id=event.venue_id
    )
    await redis.set(
        _key(event_id),
        json.dumps({
            "status": event.status.value,
            "sale_starts_at": event.sale_starts_at.isoformat(),
            "sale_ends_at": event.sale_ends_at.isoformat(),
            "price_cents": event.price_cents,
            "venue_id": event.venue_id,
            "zone_prices": zone_prices,
        }),
        ex=_TTL_SECONDS
    )
    return EventMeta(
        event_id=event_id,
        status=event.status,
        sale_starts_at=event.sale_starts_at,
        sale_ends_at=event.sale_ends_at,
        price_cents=event.price_cents,
        venue_id=event.venue_id,
        zone_prices=zone_prices,
    )

async def invalidate_event_meta(redis: Redis, *, event_id: int) -> None:
    """活動狀態改變時清掉快取(例如發佈)。

    改分區票價也必須呼叫這個 —— 否則最多 _TTL_SECONDS 內還會按舊價賣。
    """
    await redis.delete(_key(event_id))
