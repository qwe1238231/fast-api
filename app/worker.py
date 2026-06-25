"""ARQ worker — background task processor.

Run: arq app.worker.WorkerSettings
"""
import asyncio
import json
from datetime import timedelta, timezone, datetime
from uuid import UUID

from arq import cron
from arq.connections import RedisSettings
from sqlalchemy import select, delete
from sqlalchemy.exc import IntegrityError

from app.core.config import get_settings
from app.core.redis import create_redis_client
from app.db.session import AsyncSessionLocal
from app.models.order import Order, OrderStatus
from app.services.orders import expire_order
from app.crud.order import create_order, get_order_by_idempotency_key
from app.crud.refresh_token import purge_expired
from app.models.audit_log import AuditLog
from app.services.audit import AUDIT_STREAM_KEY
from app.models.event import Event, EventStatus
from app.services.inventory import compute_expected_available, get_available, release, ORDER_STREAM_KEY
from app.services.idempotency import mark_claim_failed

PENDING_TIMEOUT_MINUTES = 10
AUDIT_CONSUMER_GROUP = "audit-writer"
AUDIT_CONSUMER_NAME = "worker"
ORDER_CONSUMER_GROUP = "order-writer"
ORDER_CONSUMER_NAME = "worker"
ORDER_LOOP_CONSUMER_NAME = "stream-consumer"   # the dedicated long-lived consumer process
ORDER_DEAD_LETTER_KEY = "orders:stream:dead"
RECLAIM_IDLE_MS = 60_000   # only reclaim entries a (crashed?) consumer has held this long
MAX_DELIVERIES = 5         # give up (dead-letter) after this many delivery attempts


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

async def ensure_consumer_group(redis, stream_key: str, group: str) -> None:
    """Create a consumer group (and the stream) if it doesn't already exist."""
    try:
        await redis.xgroup_create(stream_key, group, id="0", mkstream=True)
    except Exception as exc:
        if "BUSYGROUP" not in str(exc):
            raise


async def startup(ctx: dict) -> None:
    """Open Redis client for inventory operations."""
    settings = get_settings()
    ctx["redis_client"] = create_redis_client(settings.REDIS_URL)
    await ensure_consumer_group(ctx["redis_client"], AUDIT_STREAM_KEY, AUDIT_CONSUMER_GROUP)
    await ensure_consumer_group(ctx["redis_client"], ORDER_STREAM_KEY, ORDER_CONSUMER_GROUP)

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

async def _persist_intent(fields: dict) -> str:
    """Insert one order intent. Returns 'ok' | 'duplicate' | 'failed'.

    'duplicate' -> idempotency_key already persisted (safe to ack).
    'failed'    -> a real integrity problem (e.g. bad FK) — caller must NOT ack,
                   so reclaim can retry and eventually dead-letter it.
    Transient errors (DB down, etc.) propagate as exceptions.
    """
    async with AsyncSessionLocal() as db:
        try:
            await create_order(
                db,
                user_id=int(fields["user_id"]),
                event_id=int(fields["event_id"]),
                quantity=int(fields["quantity"]),
                total_price_cents=int(fields["total_price_cents"]),
                idempotency_key=UUID(fields["idempotency_key"]),
            )
            await db.commit()
            return "ok"
        except IntegrityError:
            await db.rollback()
            existing = await get_order_by_idempotency_key(db, UUID(fields["idempotency_key"]))
            return "duplicate" if existing is not None else "failed"


async def _ack_and_remove(redis, entry_id: str) -> None:
    """Ack the entry, then delete it from the stream so it doesn't accumulate.

    XACK only clears the pending list; the entry itself lingers in the stream
    forever unless removed. Deleting once it's safely persisted keeps
    orders:stream bounded to roughly the un-processed backlog.
    """
    await redis.xack(ORDER_STREAM_KEY, ORDER_CONSUMER_GROUP, entry_id)
    await redis.xdel(ORDER_STREAM_KEY, entry_id)


async def _consume_batch(redis, *, consumer: str = ORDER_CONSUMER_NAME, block: int | None = None) -> int:
    """Drain one batch of new order intents. Returns how many entries were read.

    One DB transaction per entry; commit before ack. A genuine integrity failure
    (bad FK) is left un-acked so reclaim retries / dead-letters it.
    """
    result = await redis.xreadgroup(
        groupname=ORDER_CONSUMER_GROUP,
        consumername=consumer,
        streams={ORDER_STREAM_KEY: ">"},
        count=500,
        block=block,
    )
    if not result:
        return 0

    entries = result[0][1]
    for entry_id, fields in entries:
        try:
            outcome = await _persist_intent(fields)
            if outcome in ("ok", "duplicate"):
                await _ack_and_remove(redis, entry_id)
            # 'failed' -> leave un-acked for reclaim_stale_order_intents
        except Exception as exc:
            print(f"Failed to persist order intent {entry_id}: {exc}")
    return len(entries)


async def consume_order_intents(ctx: dict) -> None:
    """One non-blocking drain pass (used by tests and as a fallback)."""
    await _consume_batch(ctx["redis_client"], block=None)


async def run_order_consumer_loop(redis, *, block_ms: int = 2000, stop_event=None) -> None:
    """Long-lived consumer: block-wait on the stream and drain continuously.

    Run as a dedicated process (app/order_consumer.py) for near-real-time persist
    latency instead of the 1-minute cron. block_ms lets it sleep efficiently
    between bursts; stop_event allows graceful shutdown.
    """
    while stop_event is None or not stop_event.is_set():
        try:
            await _consume_batch(redis, consumer=ORDER_LOOP_CONSUMER_NAME, block=block_ms)
        except Exception as exc:
            print(f"order consumer loop error: {exc}")
            await asyncio.sleep(1)   # avoid hot-spinning on a persistent error


async def _dead_letter_intent(redis, entry_id: str, fields: dict) -> None:
    """Give up on a poison intent: park it for humans, refund the seat, mark FAILED."""
    await redis.xadd(ORDER_DEAD_LETTER_KEY, {**fields, "original_id": entry_id})
    await release(redis, event_id=int(fields["event_id"]), quantity=int(fields["quantity"]))
    await mark_claim_failed(redis, idempotency_key=fields["idempotency_key"])
    print(f"DEAD-LETTER order intent {entry_id}: {dict(fields)}")


async def reclaim_stale_order_intents(
        ctx: dict,
        *,
        min_idle_ms: int = RECLAIM_IDLE_MS,
        max_deliveries: int = MAX_DELIVERIES,
) -> None:
    """Recover intents stuck in the pending list (read but never acked — the
    consumer likely crashed). Retry each; dead-letter the poison ones.
    """
    redis = ctx["redis_client"]

    pending = await redis.xpending_range(
        ORDER_STREAM_KEY,
        ORDER_CONSUMER_GROUP,
        min="-",
        max="+",
        count=500,
        idle=min_idle_ms,
    )
    for p in pending:
        entry_id = p["message_id"]
        times_delivered = p["times_delivered"]

        claimed = await redis.xclaim(
            ORDER_STREAM_KEY,
            ORDER_CONSUMER_GROUP,
            ORDER_CONSUMER_NAME,
            min_idle_time=min_idle_ms,
            message_ids=[entry_id],
        )
        if not claimed:
            continue   # another worker grabbed it first
        _, fields = claimed[0]

        if times_delivered >= max_deliveries:
            await _dead_letter_intent(redis, entry_id, fields)
            await _ack_and_remove(redis, entry_id)
            continue

        try:
            outcome = await _persist_intent(fields)
            if outcome in ("ok", "duplicate"):
                await _ack_and_remove(redis, entry_id)
            # 'failed' -> leave; a later reclaim retries until max_deliveries, then dead-letters
        except Exception as exc:
            print(f"Reclaim failed for order intent {entry_id}: {exc}")

async def detect_inventory_drift(ctx: dict) -> list[dict]:
    """比對每個 published event 的 Redis 庫存 vs Postgres 應有值,不一致就記錄。"""
    redis = ctx["redis_client"]
    drifts: list[dict] = []
    async with AsyncSessionLocal() as db:
        result = await db.execute(
            select(Event.id).where(Event.status == EventStatus.PUBLISHED)
        )
        for event_id in result.scalars().all():
            expected = await compute_expected_available(db, event_id=event_id)
            actual = await get_available(redis, event_id=event_id)
            if expected != actual:
                drifts.append({"event_id":event_id, "expected": expected, "actual":actual})
                print(f"INVENTORY DRIFT event={event_id} redis={actual} expected={expected}")

    return drifts
        
class WorkerSettings:
    """ARQ worker config. Launch: `arq app.worker.WorkerSettings`"""

    on_startup = startup
    on_shutdown = shutdown
    
    redis_settings = RedisSettings.from_dsn(get_settings().REDIS_URL)

    cron_jobs =[
        cron(expire_pending_orders, minute={i for i in range(60)}),
        cron(purge_expired_refresh_tokens, hour={3}, minute={0}),
        cron(consume_audit_events, minute={i for i in range(60)}),
        # order intents are drained by the dedicated app.order_consumer process
        # (near-real-time); the ARQ worker only runs the reclaim safety net.
        cron(reclaim_stale_order_intents, minute={i for i in range(60)}),
        cron(purge_old_audit_logs, hour={2}, minute={30}),
        cron(detect_inventory_drift, minute=set(range(0, 60, 5))),

    ]

