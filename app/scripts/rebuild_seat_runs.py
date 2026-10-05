"""從 Postgres 重建座位的 free-run 結構 —— 座位版的 reconcile_inventory。

存在的理由:`detect_seat_structure_drift` 只**偵測**結構損壞,沒有任何東西修它。
`reconcile_inventory` 只重算 `event:{e}:available`,完全不碰 `runs` / `ends` /
zone 計數器。所以座位釋放失敗、或 drift 報出 complement 不一致時,唯一的修復
手段就是這支腳本。

    python -m app.scripts.rebuild_seat_runs <event_id> [--zone Z] [--force]

**預設拒絕在 order stream 未排空時執行。** in-flight 的 intent 持有 Redis 認定的
區間但 DB 還沒有,此時重建會把它們算成空的 —— 那些座位會被賣第二次。`--force`
只在確定沒有 in-flight intent 指向這個場次時才可以用。
"""
import argparse
import asyncio
import sys

from sqlalchemy import select

from app.core.config import get_settings
from app.core.exceptions import EventNotFound, InventoryNotReconcilable
from app.core.redis import create_redis_client
from app.db.session import AsyncSessionLocal
from app.models.event import Event
from app.models.seating import Zone
from app.services.seat_runs import rebuild_zone_runs


async def main(event_id: int, *, zone_id: int | None = None, force: bool = False) -> None:
    redis = create_redis_client(get_settings().REDIS_URL)
    try:
        async with AsyncSessionLocal() as db:
            venue_id = await db.scalar(select(Event.venue_id).where(Event.id == event_id))
            if venue_id is None:
                if not await db.scalar(select(Event.id).where(Event.id == event_id)):
                    raise EventNotFound(event_id=event_id)
                sys.exit(f"event {event_id} has no seat map — nothing to rebuild")

            zone_ids = (
                [zone_id]
                if zone_id is not None
                else list(
                    (
                        await db.scalars(
                            select(Zone.id)
                            .where(Zone.venue_id == venue_id)
                            .order_by(Zone.display_order)
                        )
                    ).all()
                )
            )
            for zid in zone_ids:
                remaining = await rebuild_zone_runs(
                    db, redis, event_id=event_id, zone_id=zid, force=force
                )
                print(f"event {event_id} zone {zid} rebuilt: available={remaining}")
    finally:
        await redis.aclose()


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("event_id", type=int)
    parser.add_argument("--zone", type=int, default=None, help="只重建這一個 zone")
    parser.add_argument(
        "--force",
        action="store_true",
        help="即使 order stream 未排空也執行 —— 只在確定沒有 in-flight intent 時使用",
    )
    args = parser.parse_args()
    try:
        asyncio.run(main(args.event_id, zone_id=args.zone, force=args.force))
    except (EventNotFound, InventoryNotReconcilable) as exc:
        sys.exit(f"rebuild_seat_runs: {exc}")
