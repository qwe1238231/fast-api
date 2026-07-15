import argparse
import asyncio
import sys

from app.core.config import get_settings
from app.core.redis import create_redis_client
from app.db.session import AsyncSessionLocal
from app.services.inventory import reconcile_inventory
from app.core.exceptions import EventNotFound, InventoryNotReconcilable


async def main(event_id: int, force: bool = False) -> None:
    redis = create_redis_client(get_settings().REDIS_URL)
    try:
        async with AsyncSessionLocal() as db:
            available = await reconcile_inventory(
                db, redis, event_id=event_id, force=force,
            )
            print(f"event {event_id} reconciled: available={available}")
    finally:
        await redis.aclose()


if __name__ == "__main__":
    parser = argparse.ArgumentParser(
        description="Rebuild an event's Redis stock counter from Postgres.",
    )
    parser.add_argument("event_id", type=int, help="event to reconcile")
    parser.add_argument(
        "--force",
        action="store_true",
        help="skip the queue-drained safety check (only when you know it's safe)",
    )
    args = parser.parse_args()

    try:
        asyncio.run(main(args.event_id, force=args.force))
    except (EventNotFound, InventoryNotReconcilable) as e:
        print(e)
        sys.exit(1)
