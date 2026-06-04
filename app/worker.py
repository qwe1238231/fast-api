"""ARQ worker — background task processor.

Run: arq app.worker.WorkerSettings
"""
from datetime import timedelta, timezone, datetime

from arq import cron
from arq.connections import RedisSettings
from sqlalchemy import select

from app.core.config import get_settings
from app.core.redis import create_redis_client
from app.db.session import AsyncSessionLocal
from app.models.order import Order, OrderStatus
from app.services.orders import expire_order
from app.crud.refresh_token import purge_expired

PENDING_TIMEOUT_MINUTES = 10


async def expire_pending_orders(ctx: dict) -> None:
    """Cron job: expire pending orders older than PENDING_TIMEOUT_MINUTES.
    
    Each order processed in its own DB transaction — one failure doesn't
    abort the batch.
    """
    cutoff = datetime.now(timezone.utc) - timedelta(minutes=PENDING_TIMEOUT_MINUTES)
    redis = ctx["redis_client"]

    async with AsyncSessionLocal() as read_db:
        stmt = (
            select(Order.id)
            .where(Order.status == OrderStatus.PENDING)
            .where(Order.created_at < cutoff)
        )
        result = await read_db.execute(stmt)
        order_ids = list(result.scalars().all())

        for order_id in order_ids:
            async with AsyncSessionLocal() as db:
                try:
                    order = await db.get(Order, order_id)
                    if order is None or order.status != OrderStatus.PENDING:
                        continue  # skip if already processed by another worker
                    await expire_order(db, redis, order)
                    await db.commit()
                except Exception as exc:
                    await db.rollback()
                    print(f"Failed to expire order {order_id}: {exc}")

async def purge_expired_refresh_tokens(ctx: dict) -> None:
    """Cron job: purge expired refresh tokens."""
    async with AsyncSessionLocal() as db:
        try:
            count = await purge_expired(db)
            await db.commit()
            if count > 0:
                print(f"Purged {count} expired refresh tokens")
        except Exception as exc:
            await db.rollback()
            print(f"Failed to purge expired tokens: {exc}")

async def startup(ctx: dict) -> None:
    """Open Redis client for inventory operations."""
    settings = get_settings()
    ctx["redis_client"] = create_redis_client(settings.REDIS_URL)


async def shutdown(ctx: dict) -> None:
    """Close Redis client."""
    await ctx["redis_client"].aclose()


class WorkerSettings:
    """ARQ worker config. Launch: `arq app.worker.WorkerSettings`"""

    on_startup = startup
    on_shutdown = shutdown
    
    redis_settings = RedisSettings.from_dsn(get_settings().REDIS_URL)

    cron_jobs =[
        cron(expire_pending_orders, minute={i for i in range(60)}),
        cron(purge_expired_refresh_tokens, hour={3}, minute={0}),
    ]