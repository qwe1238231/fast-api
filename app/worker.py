"""ARQ worker — background task processor.

Run: arq app.worker.WorkerSettings
"""
import json
from datetime import timedelta, timezone, datetime

from arq import cron
from arq.connections import RedisSettings
from sqlalchemy import select, delete

from app.core.config import get_settings
from app.core.redis import create_redis_client
from app.db.session import AsyncSessionLocal
from app.models.order import Order, OrderStatus
from app.services.orders import expire_order
from app.crud.refresh_token import purge_expired
from app.models.audit_log import AuditLog
from app.services.audit import AUDIT_STREAM_KEY


PENDING_TIMEOUT_MINUTES = 10
AUDIT_CONSUMER_GROUP = "audit-writer"
AUDIT_CONSUMER_NAME = "worker"


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

async def purge_old_audit_logs(ctx: dict) -> None:
    """Cron job: hard delete audit_logs older than retention window.
    
    GDPR data minimization — audit events past investigation horizon get purged.
    """
    settings = get_settings()
    cutoff = datetime.now(timezone.utc) - timedelta(
        days=settings.AUDIT_LOG_RETENTION_DAYS
    )
    async with AsyncSessionLocal() as db:
        try:
            result = await db.execute(
                delete(AuditLog).where(AuditLog.created_at < cutoff)
            )
            await db.commit()
            if result.rowcount > 0:
                print(
                    f"Purged {result.rowcount} audit log rows "
                    f"older than {settings.AUDIT_LOG_RETENTION_DAYS} days" 
                )
        except Exception as exc:
            await db.rollback()
            print(f"Failed to purge audit logs: {exc}")

async def startup(ctx: dict) -> None:
    """Open Redis client for inventory operations."""
    settings = get_settings()
    ctx["redis_client"] = create_redis_client(settings.REDIS_URL)
    try:
        await ctx["redis_client"].xgroup_create(
            AUDIT_STREAM_KEY,
            AUDIT_CONSUMER_GROUP,
            id="0",
            mkstream=True,
        )
    except Exception as exc:
        if "BUSYGROUP" not in str(exc):
            raise

async def shutdown(ctx: dict) -> None:
    """Close Redis client."""
    await ctx["redis_client"].aclose()

async def consume_audit_events(ctx: dict) -> None:
    """Read pending audit events from stream, batch insert to Postgres."""
    redis = ctx["redis_client"]

    result = await redis.xreadgroup(
        groupname=AUDIT_CONSUMER_GROUP,
        consumername=AUDIT_CONSUMER_NAME,
        streams={AUDIT_STREAM_KEY: ">"},
        count=10000,
        block=None,
    )
    if not result:
        return
    stream_data = result[0][1]

    async with AsyncSessionLocal() as db:
        entry_ids: list[str] = []
        for entry_id, fields in stream_data:
            audit_log = AuditLog(
                event_type=fields.get("event_type"),
                actor_user_id=(
                    int(fields["actor_user_id"]) if fields.get("actor_user_id")
                    else None
                ),
                actor_ip=fields.get("actor_ip") or None,
                target_type=fields.get("target_type") or None,
                target_id=fields.get("target_id") or None,
                payload=json.loads(fields["payload"]) if fields.get("payload") else {},
                success=fields.get("success") == "1",
                error_code=fields.get("error_code") or None,
                created_at=(
                    datetime.fromisoformat(fields["created_at"]) if fields.get("created_at")
                    else None
                ),
            )
            db.add(audit_log)
            entry_ids.append(entry_id)
        await db.commit()

    if entry_ids:
        await redis.xack(AUDIT_STREAM_KEY, AUDIT_CONSUMER_GROUP, *entry_ids)
        
class WorkerSettings:
    """ARQ worker config. Launch: `arq app.worker.WorkerSettings`"""

    on_startup = startup
    on_shutdown = shutdown
    
    redis_settings = RedisSettings.from_dsn(get_settings().REDIS_URL)

    cron_jobs =[
        cron(expire_pending_orders, minute={i for i in range(60)}),
        cron(purge_expired_refresh_tokens, hour={3}, minute={0}),
        cron(consume_audit_events, minute={i for i in range(60)}),
        cron(purge_old_audit_logs, hour={2}, minute={30}),
    ]