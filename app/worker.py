"""ARQ worker — background task processor.

Run: arq app.worker.WorkerSettings
"""
import asyncio
import functools
import json
import logging
import os
from collections.abc import Awaitable, Callable
from typing import ParamSpec, TypeVar

import boto3
from botocore.config import Config
from datetime import timedelta, timezone, datetime
from uuid import UUID

from arq import cron
from arq.connections import RedisSettings
from sqlalchemy import select, delete
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import get_settings
from app.core.logging import alert, configure_logging, log_context, new_trace_id
from app.core.redis import create_redis_client
from app.db.session import AsyncSessionLocal
from app.models.order import Order, OrderStatus
from app.models.seating import SeatBlock, SeatHold, Zone
from app.services.orders import expire_order, release_order_seat
from app.crud.order import create_order, get_order_by_idempotency_key
from app.crud.refresh_token import purge_expired
from app.crud.stripe_event import cutoff_for, purge_events_older_than
from app.models.audit_log import AuditLog
from app.services.audit import AUDIT_STREAM_KEY
from app.models.event import Event, EventStatus
from app.services.inventory import (
    compute_expected_available, compute_expected_quotas,
    oldest_intent_age_seconds, read_available, read_purchase_quotas,
    reconcile_inventory, release,
    ORDER_STREAM_KEY, ORDER_DEAD_LETTER_KEY, queue_depth,
    _key as _event_available_key, _purchased_key,
    ORDER_DEAD_LETTER_MAX_LEN,
)
from app.services.idempotency import mark_claim_failed
from app.services.seat_runs import (
    release_seats,
    _ends_key, _geom_key, _relaxed_key, _runs_key, _zone_available_key,
)
from app.services.waiting_room import (
    set_admission_paused,
    _admit_start_key, _draw_key, _salt_key,
)

logger = logging.getLogger(__name__)

_settings = get_settings()

_P = ParamSpec("_P")
_R = TypeVar("_R")


def cron_job(
    func: Callable[_P, Awaitable[_R]],
) -> Callable[_P, Awaitable[_R]]:
    """把一次 cron 執行的所有 log 綁在同一個 `job` + `run_id` 底下。

    cron **沒有入口請求可以繼承 id**,所以得自己生一個。少了它,
    `detect_seat_structure_drift` 一輪吐出來的幾十行 ALERT 會跟上一輪的混在一起,
    分不出哪些屬於同一次檢查 —— 而「這次比上次多了什麼」正是要看的東西。

    刻意**不**記「開始/結束」:arq 自己就會記每個 job 的耗時,再記一次只是把同一件事
    寫兩遍。這裡要的是 context,不是多一行。
    """

    @functools.wraps(func)
    async def wrapper(*args: _P.args, **kwargs: _P.kwargs) -> _R:
        ctx = args[0] if args else {}
        run_id = (ctx.get("job_id") if isinstance(ctx, dict) else None) or new_trace_id()
        with log_context(job=func.__name__, run_id=run_id):
            try:
                return await func(*args, **kwargs)
            except Exception:
                # arq 會記下 job 失敗,但那行沒有 run_id,接不回這次執行**裡面**
                # 那些 log。這行的價值就是把兩者接起來。
                logger.exception("cron job raised", extra={"event": "cron_failed"})
                raise

    return wrapper


PENDING_TIMEOUT_MINUTES = 10
AUDIT_CONSUMER_GROUP = "audit-writer"
AUDIT_CONSUMER_NAME = "worker"
ORDER_CONSUMER_GROUP = "order-writer"
ORDER_CONSUMER_NAME = "worker"
ORDER_LOOP_CONSUMER_NAME = "stream-consumer"   # the dedicated long-lived consumer process
RECLAIM_IDLE_MS = _settings.ORDER_RECLAIM_IDLE_MS      # only reclaim entries idle at least this long
MAX_DELIVERIES = _settings.ORDER_MAX_DELIVERIES        # dead-letter after this many delivery attempts
CONSUMER_BLOCK_MS = _settings.ORDER_CONSUMER_BLOCK_MS  # how long the loop blocks per read
BACKLOG_WARN = _settings.ORDER_BACKLOG_WARN            # log a warning above this backlog


@cron_job
async def expire_pending_orders(ctx: dict) -> None:
    """Cron job: expire pending orders older than PENDING_TIMEOUT_MINUTES.

    Each order in its own DB transaction — one failure doesn't abort the batch.
    The seat release is a POST-COMMIT, idempotent step: commit the EXPIRED
    transition first (so a rollback never leaves a freed seat), then return the
    seat. A CAS miss (the order was paid/cancelled meanwhile) is skipped, not an
    error — its new owner is responsible for the seat.
    """
    cutoff = datetime.now(timezone.utc) - timedelta(minutes=PENDING_TIMEOUT_MINUTES)
    redis = ctx["redis_client"]

    async with AsyncSessionLocal() as read_db:
        stmt = (
            select(Order.id)
            .where(Order.status == OrderStatus.PENDING)
            .where(Order.created_at < cutoff)
            # Skip orders in the Stripe flow: their lifecycle is driven by the
            # payment webhooks (succeeded -> paid; canceled/failed -> released),
            # not this timeout — so a buyer paying near the boundary keeps the
            # ticket instead of being expired out from under a successful charge.
            .where(Order.payment_provider_id.is_(None))
        )
        result = await read_db.execute(stmt)
        order_ids = list(result.scalars().all())

    for order_id in order_ids:
        # 每一筆訂單的所有 log 都掛上它的 order_id —— 一批過期是幾百筆交錯的
        # transaction,沒有這個欄位就分不出哪一行屬於哪一筆。
        with log_context(order_id=order_id):
            async with AsyncSessionLocal() as db:
                try:
                    order = await db.get(Order, order_id)
                    if order is None or order.status != OrderStatus.PENDING:
                        continue  # cheap early-out; the CAS in expire_order is the real guard
                    if not await expire_order(db, order):
                        continue  # lost the race (just paid/cancelled) — skip, don't release
                    await db.commit()
                except Exception:
                    await db.rollback()
                    # .exception 而不是 .error:堆疊才知道是哪一句炸的。舊版只留下
                    # 例外的字串,而 "connection closed" 這種訊息沒有堆疊等於沒說。
                    logger.exception(
                        "failed to expire order", extra={"event": "order_expire_failed"}
                    )
                    continue
                # committed EXPIRED — return the seat post-commit (idempotent, own step).
                # order.* stays readable after commit because expire_on_commit=False.
                try:
                    await release_order_seat(db, redis, order)
                except Exception:
                    alert(
                        logger,
                        "order expired but seat release failed — for a SEATED order "
                        "nothing repairs this automatically (reconcile_inventory only "
                        "fixes the event counter); run "
                        "`python -m app.scripts.rebuild_seat_runs <event_id>` once the "
                        "stream drains",
                        event="seat_release_failed",
                        exc_info=True,
                        event_id=order.event_id,
                    )

@cron_job
async def purge_expired_refresh_tokens(ctx: dict) -> None:
    """Cron job: purge expired refresh tokens."""
    async with AsyncSessionLocal() as db:
        try:
            count = await purge_expired(db)
            await db.commit()
            if count > 0:
                logger.info(
                    "purged expired refresh tokens",
                    extra={"event": "refresh_tokens_purged", "purged": count},
                )
        except Exception:
            await db.rollback()
            logger.exception(
                "failed to purge expired refresh tokens",
                extra={"event": "refresh_token_purge_failed"},
            )

@cron_job
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
                logger.info(
                    "purged old audit logs",
                    extra={
                        "event": "audit_logs_purged",
                        "purged": result.rowcount,
                        "retention_days": settings.AUDIT_LOG_RETENTION_DAYS,
                    },
                )
        except Exception:
            await db.rollback()
            logger.exception(
                "failed to purge audit logs",
                extra={"event": "audit_log_purge_failed"},
            )

@cron_job
async def purge_old_stripe_events(ctx: dict) -> None:
    """Cron: 清掉超過保留期的 Stripe 去重紀錄。

    這張表每一筆對應一次真實付款事件,只增不減會隨營運量無界成長 —— 而它在事件收到
    幾天後就只剩對帳價值(Stripe 最多重送三天)。跟 audit log 同一個模式。
    """
    settings = get_settings()
    cutoff = cutoff_for(settings.STRIPE_EVENT_RETENTION_DAYS, now=datetime.now(timezone.utc))
    async with AsyncSessionLocal() as db:
        try:
            deleted = await purge_events_older_than(db, cutoff=cutoff)
            await db.commit()
            if deleted:
                logger.info(
                    "purged old stripe events",
                    extra={
                        "event": "stripe_events_purged",
                        "purged": deleted,
                        "retention_days": settings.STRIPE_EVENT_RETENTION_DAYS,
                    },
                )
        except Exception:
            await db.rollback()
            logger.exception(
                "failed to purge stripe events",
                extra={"event": "stripe_event_purge_failed"},
            )


async def ensure_consumer_group(redis, stream_key: str, group: str) -> None:
    """Create a consumer group (and the stream) if it doesn't already exist."""
    try:
        await redis.xgroup_create(stream_key, group, id="0", mkstream=True)
    except Exception as exc:
        if "BUSYGROUP" not in str(exc):
            raise


async def startup(ctx: dict) -> None:
    """Open Redis client for inventory operations."""
    # arq 已經設好自己的 logging 才會呼叫這裡,所以現在覆蓋才有效(理由同 API 的
    # lifespan)。component 讓 worker 的行在 CloudWatch 上跟 api/consumer 分得開。
    configure_logging(component="ticket-worker")
    settings = get_settings()
    ctx["redis_client"] = create_redis_client(settings.REDIS_URL)
    await ensure_consumer_group(ctx["redis_client"], AUDIT_STREAM_KEY, AUDIT_CONSUMER_GROUP)
    await ensure_consumer_group(ctx["redis_client"], ORDER_STREAM_KEY, ORDER_CONSUMER_GROUP)

async def shutdown(ctx: dict) -> None:
    """Close Redis client."""
    await ctx["redis_client"].aclose()

@cron_job
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
            # zone_id / block_id 是空字串或缺欄位時代表無座位圖的場次。缺欄位的
            # 情況是為了相容:升級前就躺在 stream 裡的舊 intent 沒有這些欄位。
            zone_id = fields.get("zone_id") or None
            block_id = fields.get("block_id") or None
            if bool(zone_id) != bool(block_id):
                # 兩個判別式必須同步:建 hold 看 block_id,而 release_order_seat
                # 分流看 order.zone_id。只有一個有值的話會建出 hold 卻走計數器
                # 釋放路徑 —— 區間永遠不還,而且沒有任何警報。當成 poison 處理。
                alert(
                    logger,
                    "order intent has zone_id xor block_id — must be both or neither; "
                    "refusing to persist",
                    event="intent_fields_inconsistent",
                    zone_id=zone_id,
                    block_id=block_id,
                    fields=dict(fields),
                )
                return "failed"
            order = await create_order(
                db,
                user_id=int(fields["user_id"]),
                event_id=int(fields["event_id"]),
                zone_id=int(zone_id) if zone_id else None,
                quantity=int(fields["quantity"]),
                total_price_cents=int(fields["total_price_cents"]),
                idempotency_key=UUID(fields["idempotency_key"]),
            )
            if block_id:
                # 與 order 同一個 txn。seat_holds 的 GiST EXCLUDE 會在這裡擋下任何
                # 區間重疊 —— 那代表 Redis 的空段結構已經損壞,整個 txn 回滾、
                # 這筆 intent 走既有的 dead-letter 流程,而復原方式是
                # rebuild_zone_runs(絕不是「退還」那段區間,見該函式的說明)。
                db.add(SeatHold(
                    event_id=int(fields["event_id"]),
                    block_id=int(block_id),
                    order_id=order.id,
                    start_pos=int(fields["start_pos"]),
                    # stream 的 `quantity` 欄位同時是「張數」與「區間長度」——
                    # 配位 Lua 的 ARGV[5] 就是 length,而它被 XADD 成 quantity。
                    # 兩者今天恆等,但這是隱含耦合:若哪天出現「買 2 送 1 個座位」
                    # 之類的規則,這裡會靜默錯掉。
                    length=int(fields["quantity"]),
                ))
            await db.commit()
            return "ok"
        except IntegrityError as exc:
            await db.rollback()
            if "ex_seat_holds_no_overlap" in str(exc):
                # 座位區間重疊 —— Redis 的空段結構已經損壞,而且它把重疊的座位發給了
                # 兩個買家。這個訊息**非零就是 bug**,沒有可容忍的常態值。復原方式是
                # rebuild_zone_runs(絕不能退還這段區間:無法判斷重疊的哪一段是誰的,
                # 盲目退還會把別人持有的座位標回可賣)。
                alert(
                    logger,
                    "seat hold conflict — overlapping interval rejected by the DB; the "
                    "zone's run structure is corrupt and needs rebuild_zone_runs",
                    event="seat_hold_conflict",
                    fields=dict(fields),
                )
            existing = await get_order_by_idempotency_key(db, UUID(fields["idempotency_key"]))
            return "duplicate" if existing is not None else "failed"


async def _ack_and_remove(redis, entry_id: str) -> None:
    """Ack + delete the entry ATOMICALLY (MULTI/EXEC).

    XACK only clears the pending list; the entry lingers in the stream until
    XDEL. Doing them as two separate calls lets a crash in between leave an
    acked-but-undeleted ORPHAN that XLEN counts as backlog forever — which then
    wedges the drift/reconcile guards. MULTI/EXEC runs both or neither.
    """
    async with redis.pipeline(transaction=True) as pipe:
        pipe.xack(ORDER_STREAM_KEY, ORDER_CONSUMER_GROUP, entry_id)
        pipe.xdel(ORDER_STREAM_KEY, entry_id)
        await pipe.execute()


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
        # **這裡就是跨 process 的接回點。** API 那邊的 contextvar 到不了這個進程,
        # 但 trace_id 跟著訊息走了 —— 掛回自己的 context,底下所有 log(包含
        # _persist_intent 裡的 ALERT)就自動接回當初那個請求。
        #
        # 用 `with` 而不是手動 set/reset 是刻意的:一批 500 筆在同一個 Task 上依序
        # 處理,漏掉 reset 的話上一筆的 trace_id 會冒充成下一筆的(尤其是升級前就
        # 躺在 stream 裡、沒有這個欄位的舊 intent)。那種 bug 的症狀是「log 看起來
        # 完全合理但指向錯的訂單」。
        with log_context(
            trace_id=fields.get("trace_id") or "-",
            idempotency_key=fields.get("idempotency_key"),
            stream_id=entry_id,
        ):
            try:
                outcome = await _persist_intent(fields)
                if outcome in ("ok", "duplicate"):
                    await _ack_and_remove(redis, entry_id)
                    # DEBUG 而不是 INFO:尖峰時這是每秒幾百行,常態下純粹是噪音跟
                    # CloudWatch 帳單。但查單一筆訂單時它是「到底有沒有落帳」的直接
                    # 證據,所以留著,由 LOG_LEVEL 決定要不要開。
                    logger.debug(
                        "order intent persisted",
                        extra={"event": "intent_persisted", "outcome": outcome},
                    )
                else:
                    # 'failed' -> leave un-acked for reclaim_stale_order_intents.
                    # 這裡以前是完全靜默的:一筆 poison intent 被丟著不 ack,而在
                    # reclaim 累積到 5 次投遞、終於死信之前(最少 5 分鐘),log 上
                    # 沒有任何痕跡 —— 那正是「使用者拿到 202 卻沒有訂單」的空窗期。
                    logger.warning(
                        "order intent rejected; left un-acked for reclaim",
                        extra={"event": "intent_persist_rejected"},
                    )
            except Exception:
                logger.exception(
                    "failed to persist order intent",
                    extra={"event": "intent_persist_failed"},
                )
    return len(entries)


@cron_job
async def consume_order_intents(ctx: dict) -> None:
    """One non-blocking drain pass (used by tests and as a fallback)."""
    await _consume_batch(ctx["redis_client"], block=None)


async def run_order_consumer_loop(redis, *, block_ms: int = CONSUMER_BLOCK_MS, stop_event=None) -> None:
    """Long-lived consumer: block-wait on the stream and drain continuously.

    Run as a dedicated process (app/order_consumer.py) for near-real-time persist
    latency instead of the 1-minute cron. block_ms lets it sleep efficiently
    between bursts; stop_event allows graceful shutdown.
    """
    while stop_event is None or not stop_event.is_set():
        try:
            await _consume_batch(redis, consumer=ORDER_LOOP_CONSUMER_NAME, block=block_ms)
        except Exception:
            logger.exception(
                "order consumer loop error", extra={"event": "consumer_loop_error"}
            )
            await asyncio.sleep(1)   # avoid hot-spinning on a persistent error


async def _dead_letter_intent(redis, entry_id: str, fields: dict) -> None:
    """Give up on a poison intent: park it for humans, refund the seat IFF it was
    never persisted, mark the claim FAILED.

    The refund is guarded two ways so it can never oversell:
      - DB existence check: if the order row already exists (it committed but the
        consumer crashed before ack, then reclaim exhausted its deliveries), the
        order legitimately holds its seat -> do NOT refund.
      - idempotent release (marker dl:{key}): a replayed dead-letter (crash before
        the ack) can't double-refund.
    (A replay can still XADD a duplicate dead-letter entry — cosmetic; the dead
    stream's lifecycle is batch 2.)
    """
    idem = fields["idempotency_key"]
    async with AsyncSessionLocal() as db:
        already_persisted = await get_order_by_idempotency_key(db, UUID(idem)) is not None

    await redis.xadd(
        ORDER_DEAD_LETTER_KEY,
        {**fields, "original_id": entry_id},
        maxlen=ORDER_DEAD_LETTER_MAX_LEN,
        approximate=True,
    )
    if not already_persisted:
        block_id = fields.get("block_id") or None
        if block_id:
            # 座位場次:要還的是一段具體區間,不是一個數量。intent 從未落帳所以
            # DB 沒有對應的 hold 列可刪 —— 直接把區間還回 Redis 就好。
            zone_id = fields.get("zone_id")
            if not zone_id:
                # _persist_intent 會把「只有一個」當 poison 拒絕,所以到不了這裡;
                # 但 dead-letter 是獨立路徑,不該用 fields["zone_id"] 直接索引而
                # 在收尾流程裡炸出 KeyError。
                alert(
                    logger,
                    "dead-letter intent has block_id but no zone_id — seats cannot be "
                    "returned automatically",
                    event="dead_letter_fields_inconsistent",
                    fields=dict(fields),
                )
                await mark_claim_failed(redis, idempotency_key=idem)
                return
            await release_seats(
                redis,
                event_id=int(fields["event_id"]),
                zone_id=int(zone_id),
                user_id=int(fields["user_id"]),
                block_id=int(block_id),
                start_pos=int(fields["start_pos"]),
                length=int(fields["quantity"]),
                marker=f"dl:{idem}",
            )
        else:
            await release(
                redis,
                event_id=int(fields["event_id"]),
                user_id=int(fields["user_id"]),
                quantity=int(fields["quantity"]),
                marker=f"dl:{idem}",
            )
    await mark_claim_failed(redis, idempotency_key=idem)
    # 刻意**不**走 alert():死信的告警掛在 order_dead_letter_new 這個數值指標上
    # (它會在事故結束後自己 OK),這裡多掛一個 needs_human 只會讓同一件事叫兩次。
    logger.error(
        "dead-lettered order intent",
        extra={
            "event": "order_dead_lettered",
            "already_persisted": already_persisted,
            "fields": dict(fields),
        },
    )


@cron_job
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
            continue
        _, fields = claimed[0]

        # 跟 _consume_batch 同一個接回動作 —— 重試與死信也要落在原本那條線上,
        # 否則「這筆訂單後來怎麼了」在 log 裡就斷在第一次失敗。
        with log_context(
            trace_id=fields.get("trace_id") or "-",
            idempotency_key=fields.get("idempotency_key"),
            stream_id=entry_id,
            times_delivered=times_delivered,
        ):
            if times_delivered >= max_deliveries:
                await _dead_letter_intent(redis, entry_id, fields)
                await _ack_and_remove(redis, entry_id)
                continue

            try:
                outcome = await _persist_intent(fields)
                if outcome in ("ok", "duplicate"):
                    await _ack_and_remove(redis, entry_id)
            except Exception:
                logger.exception(
                    "reclaim failed for order intent",
                    extra={"event": "intent_reclaim_failed"},
                )

#: 斷路器讀到哪一筆死信了。存的是 stream id,不是筆數 —— 筆數會被 maxlen 修剪弄髒。
DEAD_LETTER_CURSOR_KEY = "orders:stream:dead:breaker_cursor"


async def count_new_dead_letters(redis, *, cap: int) -> int:
    """自上次呼叫以來新增了幾筆死信(數到 cap + 1 就夠了,不會掃整條)。

    斷路器要的是**速率**而不是總量。`XLEN` 只增不減(沒有人會清死信),所以拿它比
    門檻的話,歷史上壞過一次就永遠跳閘、整站永久停售 —— 而且外觀跟「系統正在保護
    自己」一模一樣,沒有人會覺得該去看。

    兩個容易寫錯的地方:
    - **第一次要對齊游標並回報 0。** 不然 worker 每次重啟都會把整段歷史當成「剛剛
      新增的」而立刻跳閘 —— 就是同一個 bug 換一個入口回來。
    - **游標一律推進到當下最新的一筆**,不是只推進我們數到的那 cap + 1 筆。否則一次
      湧入一萬筆之後,每分鐘都還會數到滿額,斷路器在事故結束後又多開一百分鐘。
    """
    cursor = await redis.get(DEAD_LETTER_CURSOR_KEY)
    newest = await redis.xrevrange(ORDER_DEAD_LETTER_KEY, count=1)
    newest_id = newest[0][0] if newest else "0-0"

    if cursor is None:
        await redis.set(DEAD_LETTER_CURSOR_KEY, newest_id)
        return 0

    entries = await redis.xrange(
        ORDER_DEAD_LETTER_KEY, min=f"({cursor}", max=newest_id, count=cap + 1
    )
    await redis.set(DEAD_LETTER_CURSOR_KEY, newest_id)
    return len(entries)


async def collect_queue_stats(redis) -> dict:
    """Current order-queue depths.

    backlog = unprocessed intents still in the stream (總量是對的:佇列深度,消費者
    追上就會降);dead_letter = 累計放棄的筆數(給儀表板看趨勢);new_dead_letters =
    自上次檢查以來新增的筆數(**斷路器與告警看這個**,理由見 count_new_dead_letters)。
    """
    cap = get_settings().ADMISSION_PAUSE_NEW_DEAD_LETTERS
    return {
        "backlog": await redis.xlen(ORDER_STREAM_KEY),
        "dead_letter": await redis.xlen(ORDER_DEAD_LETTER_KEY),
        "new_dead_letters": await count_new_dead_letters(redis, cap=cap),
        # backlog 的**筆數**回答「積了多少」;年紀回答「積了多久」,而後者才是
        # 耐久性的曝險窗 —— 見 oldest_intent_age_seconds。
        "oldest_intent_age": await oldest_intent_age_seconds(redis),
    }

METRIC_NAMESPACE_ENV_VAR = "PIPELINE_METRIC_NAMESPACE"
METRIC_NAME_BACKLOG = "order_stream_backlog"
METRIC_NAME_DEAD_LETTER = "order_dead_letter_depth"      # 累計:給儀表板看趨勢
METRIC_NAME_DEAD_LETTER_NEW = "order_dead_letter_new"    # 每分鐘新增:**告警看這個**
METRIC_NAME_SALE_IMMINENT = "sale_imminent"              # 0/1:api 的預熱擴容看這個
#: 最舊那筆未落帳 intent 的年紀。**這是耐久性的曝險窗**:Redis 此刻死掉會丟掉的,
#: 就是這段時間內收下的訂單。副本只能保護最後幾百毫秒,所以這個數字必須是被監控的。
METRIC_NAME_OLDEST_INTENT_AGE = "order_stream_oldest_age_seconds"

_METRIC_NAMESPACE = os.environ.get(METRIC_NAMESPACE_ENV_VAR)
# Build the client only when BOTH env vars are present (the deployed worker task
# def injects them together); either missing (e.g. local dev) → disabled. This
# way a missing AWS_REGION degrades to "no metrics" instead of a NoRegionError at
# import that would stop the worker booting. Short timeouts + no retries: a
# CloudWatch stall must never wedge this once-a-minute cron (it also drives the
# circuit breaker). Region is read from AWS_REGION by boto3.
_cloudwatch = (
    boto3.client(
        "cloudwatch",
        config=Config(connect_timeout=2, read_timeout=3, retries={"total_max_attempts": 1}),
    )
    if _METRIC_NAMESPACE and os.environ.get("AWS_REGION")
    else None
)

async def _put_gauges(values: dict[str, float]) -> None:
    """把一組 gauge 送去 CloudWatch。沒有設定 namespace/region 時是 no-op(本機)。

    一次 PutMetricData 送完所有指標,不是每個指標一次呼叫:CloudWatch 按**呼叫次數**
    計費,而這是每分鐘都在跑的 cron。
    """
    if _cloudwatch is None:
        return
    try:
        await asyncio.to_thread(
            _cloudwatch.put_metric_data,
            Namespace=_METRIC_NAMESPACE,
            MetricData=[
                {"MetricName": name, "Value": value, "Unit": "Count"}
                for name, value in values.items()
            ],
        )
    except Exception:
        logger.warning(
            "metric publish failed",
            exc_info=True,
            extra={"event": "metric_publish_failed", "metrics": sorted(values)},
        )


async def _publish_pipeline_gauges(
    backlog: int, dead_letter: int, new_dead_letters: int, oldest_intent_age: float = 0.0
) -> None:
    """死信發兩個指標:累計與每分鐘新增。

    告警必須掛在**新增**上。累計值只增不減,門檻 0 的告警一旦響過就永遠在 ALARM ——
    而那跟斷路器永久跳閘是同一個病:一個永遠在響的告警等於沒有告警,下次真的出事
    沒有人會注意到。累計值留著是因為儀表板上的趨勢線仍然有用。
    """
    await _put_gauges({
        METRIC_NAME_BACKLOG: backlog,
        METRIC_NAME_DEAD_LETTER: dead_letter,
        METRIC_NAME_DEAD_LETTER_NEW: new_dead_letters,
        METRIC_NAME_OLDEST_INTENT_AGE: oldest_intent_age,
    })



@cron_job
async def report_queue_depth(ctx: dict) -> dict:
    """Cron: log a smoke-alarm when the dead-letter stream is non-empty or the
    backlog is climbing. (Grafana reads the same depths via the API /metrics.)"""
    stats = await collect_queue_stats(ctx["redis_client"])
    await _publish_pipeline_gauges(
        stats["backlog"],
        stats["dead_letter"],
        stats["new_dead_letters"],
        stats["oldest_intent_age"],
    )
    # 告警看新增而不是總量:總量 > 0 會在第一次事故之後每分鐘叫到天荒地老,而一個
    # 永遠在響的告警等於沒有告警。
    if stats["new_dead_letters"] > 0:
        alert(
            logger,
            "new order dead-letters this minute — orders failing permanently",
            event="dead_letters_new",
            new_dead_letters=stats["new_dead_letters"],
            dead_letter_total=stats["dead_letter"],
        )
    if stats["backlog"] > BACKLOG_WARN:
        logger.warning(
            "order backlog high — consumers may be falling behind",
            extra={"event": "order_backlog_high", "backlog": stats["backlog"]},
        )

    # Circuit breaker: pause the waiting room's admission when the order pipeline is
    # unhealthy, so we stop feeding new buyers into a system that can't keep up.
    settings = get_settings()
    unhealthy = (
        stats["new_dead_letters"] > settings.ADMISSION_PAUSE_NEW_DEAD_LETTERS
        or stats["backlog"] > settings.ADMISSION_PAUSE_BACKLOG_THRESHOLD
    )
    await set_admission_paused(ctx["redis_client"], unhealthy)
    if unhealthy:
        logger.error(
            "circuit breaker: admission paused",
            extra={
                "event": "admission_paused",
                "backlog": stats["backlog"],
                "new_dead_letters": stats["new_dead_letters"],
            },
        )
    return stats


async def find_imminent_sale(
    db: AsyncSession,
    *,
    now: datetime,
    lead: timedelta,
    tail: timedelta,
) -> int | None:
    """回傳「即將/正在開賣」的最近一個 published 場次 id,沒有就 None。

    定義是一個以 `sale_starts_at` 為錨的窗:`[開賣 − lead, 開賣 + tail]`。

    **為什麼錨在開賣時間而不是等候室的開放時間**:`queue_opens_at` 可以被設成任意早
    (它是每場次可覆寫的欄位),而「排隊已經開放」不等於「負載已經來了」—— 註冊只是
    一次 POST。真正把資源吃掉的是開賣前後那段所有人都掛著 SSE、然後一起送單的時間,
    而那恆定地繞著開賣時刻。用可被任意設定的欄位當錨,代價是有人填錯就讓叢集用最大
    容量跑好幾天。

    所以 lead **必須大於等候室的預設提前量**(`QUEUE_LEAD_TIME_SECONDS`),否則排隊
    一開放就有人湧入、而擴容還沒發生 —— Settings 有 validator 釘住這個關係。
    """
    return await db.scalar(
        select(Event.id)
        .where(
            Event.status == EventStatus.PUBLISHED,
            # 等價於 `now ∈ [開賣 − lead, 開賣 + tail]`,但寫成對 sale_starts_at 的
            # 範圍條件 —— 這樣資料庫端是一個可用索引的範圍掃描,而不是逐列算日期。
            Event.sale_starts_at <= now + lead,
            Event.sale_starts_at >= now - tail,
        )
        .order_by(Event.sale_starts_at)
        .limit(1)
    )


@cron_job
async def publish_prewarm_signal(ctx: dict) -> int:
    """每分鐘發一個 0/1 的 `sale_imminent`,api 的預熱擴容掛在它上面。

    **為什麼要這個訊號**:搶票的尖峰是**秒級**的,而 ECS 的反應式擴容是分鐘級 ——
    等 CPU 高了才擴,尖峰已經過了(而且過的方式是使用者吃 503)。所以真正有用的是
    **在已知的時刻之前**先把容量拉上去。開賣時間只存在資料庫裡,Terraform 看不到,
    所以只能由這裡把它變成一個 CloudWatch 指標。

    **為什麼是「發指標」而不是直接改 autoscaling 的 min_capacity**:min_capacity 是
    Terraform 擁有的屬性,程式去改它會讓下一次 apply 默默把它改回來 —— 這個專案已經
    在 `task_definition` 與 `desired_count` 上被同一個坑咬過兩次。發指標讓職責乾淨:
    app 只回報事實,容量的上下界仍然只有 Terraform 一個宣告點。

    **0 也要發。** 只在為真時發的話,平時沒有資料點,告警會停在 INSUFFICIENT_DATA
    而不是 OK —— 跟 log metric filter 需要 `default_value = 0` 是同一件事。
    """
    settings = get_settings()
    now = datetime.now(timezone.utc)
    async with AsyncSessionLocal() as db:
        event_id = await find_imminent_sale(
            db,
            now=now,
            lead=timedelta(minutes=settings.PREWARM_LEAD_MINUTES),
            tail=timedelta(minutes=settings.PREWARM_TAIL_MINUTES),
        )

    imminent = int(event_id is not None)
    await _put_gauges({METRIC_NAME_SALE_IMMINENT: imminent})
    if imminent:
        logger.info(
            "sale window is imminent — holding pre-warmed capacity",
            extra={"event": "sale_imminent", "event_id": event_id},
        )
    return imminent


@cron_job
async def detect_inventory_drift(ctx: dict) -> list[dict]:
    """比對每個 published event 的 Redis 庫存 vs Postgres 應有值。

    兩種不一致,處理方式**刻意不同**:

      鍵不存在  → Redis 把狀態弄丟了(節點被換掉、有人清了 keyspace、故障切換丟了
                  最後那段寫入)。這種情況 Postgres 是無爭議的權威,而且症狀很惡劣:
                  `get_available` 把缺鍵折成 0,所以那場**看起來完售**,所有計數器
                  自洽,沒有任何訊號會亮。**自動重建**,並且用 needs_human 記一筆。
      值對不上  → 可能是程式的 bug(某條釋放路徑漏了)。這種**只告警不自動修** ——
                  自動修會把 bug 的症狀擦掉,於是它每五分鐘被修好一次、永遠不會被查。
    """
    redis = ctx["redis_client"]
    settings = get_settings()
    backlog, _ = await queue_depth(redis)   # only un-persisted backlog blocks drift;
    if backlog:                             # dead-lettered intents are inventory-settled (batch 1)
        logger.info(
            "drift check skipped: intents not yet persisted",
            extra={"event": "drift_check_skipped", "backlog": backlog},
        )
        return []
    drifts: list[dict] = []
    # **只看「鍵還應該存在」的場次**,也就是 purge_finished_event_keys 的保留窗之內。
    # 少了這個過濾,早就被清掉的場次每五分鐘都會被報成一次庫存漂移(Redis 沒有鍵 → 0,
    # 而 Postgres 還算得出未售出的張數)—— 一個永遠在響、而且完全正確地響的假警報。
    # detect_seat_structure_drift 一直有這個過濾,這裡漏了。
    horizon = datetime.now(timezone.utc) - timedelta(days=EVENT_KEY_RETENTION_DAYS)
    async with AsyncSessionLocal() as db:
        result = await db.execute(
            select(Event.id).where(
                Event.status == EventStatus.PUBLISHED,
                Event.ends_at >= horizon,
            )
        )
        for event_id in result.scalars().all():
            expected = await compute_expected_available(db, event_id=event_id)
            actual = await read_available(redis, event_id=event_id)
            if actual is None:
                drifts.append({
                    "kind": "lost", "event_id": event_id, "expected": expected,
                })
                alert(
                    logger,
                    "redis lost this event's inventory key — the event silently looks "
                    "SOLD OUT; rebuilding from Postgres",
                    event="redis_state_lost",
                    event_id=event_id,
                    expected=expected,
                    healed=settings.AUTO_HEAL_LOST_REDIS_STATE,
                )
                if settings.AUTO_HEAL_LOST_REDIS_STATE:
                    # backlog 已經是 0(上面守過),所以 Postgres 就是此刻的全部事實。
                    # reconcile 會一起重建限購額度 —— 兩者必須從同一次讀出來的事實推導。
                    await reconcile_inventory(db, redis, event_id=event_id)
            elif expected != actual:
                drifts.append({"event_id":event_id, "expected": expected, "actual":actual})
                logger.error(
                    "inventory drift: Redis and Postgres disagree on remaining stock",
                    extra={
                        "event": "inventory_drift",
                        "event_id": event_id,
                        "redis": actual,
                        "expected": expected,
                    },
                )

            # 限購額度也要對帳。它的漂移**比庫存漂移更難發現**:超賣會撞到總量、
            # 等候室會叫;超買不會撞到任何東西 —— 那個人就是多買了幾張,而所有計數器
            # 都自洽。所以這裡是唯一會看到它的地方。
            #
            # 只記「誰對不上」而不是整份 hash:一個熱門場次有幾萬個買家,把整份倒進
            # log 只會讓真正的訊號被淹掉。
            expected_quotas = await compute_expected_quotas(db, event_id=event_id)
            actual_quotas = await read_purchase_quotas(redis, event_id=event_id)
            mismatched = {
                user_id: (actual_quotas.get(user_id, 0), expected_quotas.get(user_id, 0))
                for user_id in expected_quotas.keys() | actual_quotas.keys()
                if actual_quotas.get(user_id, 0) != expected_quotas.get(user_id, 0)
            }
            if mismatched:
                drifts.append({
                    "kind": "quota", "event_id": event_id,
                    "users": len(mismatched),
                    # 只舉幾個例子,足夠讓人去查。
                    "sample": dict(list(mismatched.items())[:5]),
                })
                logger.error(
                    "quota drift: per-user purchase quotas disagree with Postgres",
                    extra={
                        "event": "quota_drift",
                        "event_id": event_id,
                        "users": len(mismatched),
                        # 值是 (redis, expected) 的 tuple
                        "sample": {
                            k: list(v) for k, v in list(mismatched.items())[:5]
                        },
                    },
                )

    return drifts
        
#: 活動結束後保留 Redis key 的天數。退款/取消到這時都走完了,留一週給事後對帳。
EVENT_KEY_RETENTION_DAYS = 7

#: 只清「結束於 [now − WINDOW, now − RETENTION]」之間的場次。更舊的已經被前幾天的
#: 排程清掉了,所以這個上界讓每天的工作量有界(而不是每天重掃整個 events 表)。
#: 代價:worker 停機超過 (WINDOW − RETENTION) 天的話,那段期間的 key 會永久留下 ——
#: 屆時手動放大 retention_days 的下界重跑一次即可。
EVENT_KEY_PURGE_WINDOW_DAYS = 30


@cron_job
async def purge_finished_event_keys(
    ctx: dict,
    *,
    retention_days: int = EVENT_KEY_RETENTION_DAYS,
    window_days: int = EVENT_KEY_PURGE_WINDOW_DAYS,
) -> int:
    """清掉已結束場次的 Redis key。回傳刪掉幾個 key。

    這些 key 全部沒有 TTL,因為它們在銷售期間必須一直存在:給 `available` 設 TTL
    會讓庫存在開賣中途消失。所以只能靠排程清。

    **不用 SCAN。** key 的集合完全可以從 Postgres 推導(event id × zone id × 固定
    後綴),所以枚舉 DB 就好 —— SCAN 在大 keyspace 上是 O(全部 key),而且會跟熱路徑
    搶 Redis 的時間。

    `queue:{e}:salt` 也在清單裡但要小心:重新產生它會讓抽籤順序重排。場次結束
    七天後不可能還有人在排隊,所以安全。
    """
    redis = ctx["redis_client"]
    now = datetime.now(timezone.utc)
    upper = now - timedelta(days=retention_days)
    lower = now - timedelta(days=window_days)

    keys: list[str] = []
    async with AsyncSessionLocal() as db:
        rows = (
            await db.execute(
                select(Event.id, Zone.id)
                .outerjoin(Zone, Zone.venue_id == Event.venue_id)
                .where(Event.ends_at < upper, Event.ends_at >= lower)
            )
        ).all()

        for event_id, zone_id in rows:
            # 用 waiting_room 自己的 helper 而不是重打字串:上一版漏掉了
            # queue:{e}:admit_start,而漏掉的原因正是憑印象打 key 格式。
            keys += [
                _event_available_key(event_id),
                _purchased_key(event_id),
                _salt_key(event_id),
                _draw_key(event_id),
                _admit_start_key(event_id),
            ]
            if zone_id is not None:
                keys += [
                    _runs_key(event_id, zone_id),
                    _ends_key(event_id, zone_id),
                    _geom_key(event_id, zone_id),
                    _zone_available_key(event_id, zone_id),
                    _relaxed_key(event_id, zone_id),
                ]

    # event:{e}:meta 不在清單裡 —— 它本來就有 60 秒 TTL,早就自己走了。
    if not keys:
        return 0
    # 分批:一次 DEL 幾千個 key 會阻塞 Redis 的單執行緒。
    unique = sorted(set(keys))
    deleted = 0
    for offset in range(0, len(unique), 500):
        deleted += await redis.delete(*unique[offset : offset + 500])
    if deleted:
        logger.info(
            "purged Redis keys for finished events",
            extra={
                "event": "event_keys_purged",
                "deleted": deleted,
                "finished_before": upper.date().isoformat(),
            },
        )
    return deleted


@cron_job
async def detect_seat_structure_drift(ctx: dict) -> list[dict]:
    """檢查座位 free-run 結構的四條不變式。**預防勝於復原**。

    結構一旦損壞,最終會表現成 worker 落帳時撞上 seat_holds 的 EXCLUDE —— 但那已經
    太晚:那時 Redis 已經把重疊的座位發給兩個買家,而復原只能整個 zone 重建
    (`python -m app.scripts.rebuild_seat_runs`)。這些檢查是同一件事的早期訊號。

    四條的性質不同,所以 backlog 的門檻也不同:

      index       runs 與 ends 必須互為索引。純 Redis 內部,backlog 再多也照查 ——
                  而且它是最早的訊號(ends 壞掉時 runs 還看起來完全正常,要等下一次
                  合併才會把兩個 run 併成宣稱同一批座位的一個)。
      counter     zone available 必須等於各空段長度之和。同樣純 Redis,照查。
      event_total event available 必須等於各 zone available 之和。三條 per-zone 檢查
                  都看不到它(counter 只比對單一 zone 內部),而 reconcile_inventory
                  只修 event 計數器,跑完就會製造這種不一致。
      complement  runs 必須是 DB 已佔用區間的補集。**要等 stream 排空** ——
                  in-flight 的 intent 持有 Redis 認定的區間但 DB 還沒有,不等就誤報。

    **DB session 刻意不跨越 Redis 往返。** 每個場次開一次短 session 把要比對的資料
    撈完就關,之後才碰 Redis —— 一條 pooled 連線被抓著幾分鐘會把 vacuum 的 xmin
    horizon 一起釘住(見 webhook 裡同一條規則)。

    也只檢查「key 還應該存在」的場次,也就是 purge_finished_event_keys 的保留窗口
    之內。少了這個過濾,兩年前結束的場次會永遠每 5 分鐘被檢查一次,成本隨歷史無界。
    """
    redis = ctx["redis_client"]
    backlog, _ = await queue_depth(redis)
    horizon = datetime.now(timezone.utc) - timedelta(days=EVENT_KEY_RETENTION_DAYS)
    drifts: list[dict] = []

    async with AsyncSessionLocal() as db:
        event_ids = list(
            (
                await db.scalars(
                    select(Event.id).where(
                        Event.status == EventStatus.PUBLISHED,
                        Event.venue_id.is_not(None),
                        Event.ends_at >= horizon,
                    )
                )
            ).all()
        )

    for event_id in event_ids:
        # 一個場次一次短 session:三個查詢撈完就關,不跨 Redis 往返。
        async with AsyncSessionLocal() as db:
            venue_id = await db.scalar(select(Event.venue_id).where(Event.id == event_id))
            blocks = (
                await db.execute(
                    select(SeatBlock.id, SeatBlock.zone_id, SeatBlock.capacity)
                    .join(Zone, Zone.id == SeatBlock.zone_id)
                    .where(Zone.venue_id == venue_id)
                )
            ).all()
            zone_ids = list(
                (
                    await db.scalars(select(Zone.id).where(Zone.venue_id == venue_id))
                ).all()
            )
            held = (
                await db.execute(
                    select(SeatHold.block_id, SeatHold.start_pos, SeatHold.length)
                    .where(SeatHold.event_id == event_id)
                )
            ).all()

        blocks_by_zone: dict[int, list[tuple[int, int]]] = {zid: [] for zid in zone_ids}
        for block_id, zone_id, capacity in blocks:
            blocks_by_zone[zone_id].append((block_id, capacity))
        occupied: dict[int, list[tuple[int, int]]] = {}
        for block_id, start, length in held:
            occupied.setdefault(block_id, []).append((start, start + length))

        # 一趟 pipeline 讀完這個場次所有 zone 的三個 key。
        async with redis.pipeline(transaction=True) as pipe:
            for zone_id in zone_ids:
                pipe.hgetall(_runs_key(event_id, zone_id))
                pipe.hgetall(_ends_key(event_id, zone_id))
                pipe.get(_zone_available_key(event_id, zone_id))
            pipe.get(_event_available_key(event_id))
            results = await pipe.execute()
        event_counter = results[-1]

        zone_sum = 0
        for index, zone_id in enumerate(zone_ids):
            runs_raw, ends_raw, counter = results[index * 3 : index * 3 + 3]

            expected_ends = {
                f"{field.split(':')[0]}:{int(field.split(':')[1]) + int(length)}":
                    field.split(":")[1]
                for field, length in runs_raw.items()
            }
            if ends_raw != expected_ends:
                drifts.append({"kind": "index", "event_id": event_id, "zone_id": zone_id})
                alert(
                    logger,
                    "seat index drift — runs/ends 不一致,結構已損壞;修復:"
                    f"python -m app.scripts.rebuild_seat_runs {event_id} --zone {zone_id}",
                    event="seat_index_drift",
                    event_id=event_id,
                    zone_id=zone_id,
                )

            total = sum(int(length) for length in runs_raw.values())
            zone_sum += int(counter or 0)
            if counter is not None and int(counter) != total:
                drifts.append({
                    "kind": "counter", "event_id": event_id, "zone_id": zone_id,
                    "expected": total, "actual": int(counter),
                })
                alert(
                    logger,
                    "seat counter drift — zone available 不等於各空段長度之和",
                    event="seat_counter_drift",
                    event_id=event_id,
                    zone_id=zone_id,
                    redis=int(counter),
                    sum_of_runs=total,
                )

            if backlog:
                continue        # 補集檢查會誤報,跳過(上面兩條已經查完了)

            expected_runs: dict[str, str] = {}
            for block_id, capacity in blocks_by_zone[zone_id]:
                cursor = 0
                for lo, hi in sorted(occupied.get(block_id, [])):
                    if lo > cursor:
                        expected_runs[f"{block_id}:{cursor}"] = str(lo - cursor)
                    cursor = max(cursor, hi)
                if cursor < capacity:
                    expected_runs[f"{block_id}:{cursor}"] = str(capacity - cursor)

            if runs_raw != expected_runs:
                drifts.append({
                    "kind": "complement", "event_id": event_id, "zone_id": zone_id,
                })
                alert(
                    logger,
                    "seat structure drift — Redis 空段不等於 DB 佔用的補集;修復:"
                    f"python -m app.scripts.rebuild_seat_runs {event_id} --zone {zone_id}",
                    event="seat_structure_drift",
                    event_id=event_id,
                    zone_id=zone_id,
                )

        if event_counter is not None and int(event_counter) != zone_sum:
            drifts.append({
                "kind": "event_total", "event_id": event_id,
                "expected": zone_sum, "actual": int(event_counter),
            })
            alert(
                logger,
                "seat event-total drift — event available 不等於各 zone available 之和",
                event="seat_event_total_drift",
                event_id=event_id,
                event_counter=int(event_counter),
                sum_of_zones=zone_sum,
            )

    return drifts


class WorkerSettings:
    """ARQ worker config. Launch: `arq app.worker.WorkerSettings`"""

    on_startup = startup
    on_shutdown = shutdown

    redis_settings = RedisSettings.from_dsn(get_settings().REDIS_URL)

    #: 同時最多跑幾個 job。**這個數字是連線預算的一部分,不是隨手填的。**
    #:
    #: 每分鐘那一刻有四個 cron 同時到期(expire / audit / reclaim / report),每 5 分鐘
    #: 再多兩個漂移偵測,而每個 job 會開 1~2 條 DB 連線。ARQ 的預設是 10,也就是
    #: worker 的連線用量上界是「10 × 每個 job 的 session 數」—— 那是一個**沒有人宣告過
    #: 的**上界,而 autoscaling 的 max_capacity 是拿整體連線預算算出來的。把它明確定成
    #: 4,worker 那一格才是一個真的數字而不是猜的。
    #:
    #: 代價:同一刻到期的第五個 cron 要等前面的讓出位子。它們都是秒級的,而 cron 的
    #: 語意本來就是「這一分鐘內跑」而不是「這一秒跑」。
    max_jobs = 4

    cron_jobs =[
        cron(expire_pending_orders, minute={i for i in range(60)}),
        cron(purge_expired_refresh_tokens, hour={3}, minute={0}),
        cron(consume_audit_events, minute={i for i in range(60)}),
        # order intents are drained by the dedicated app.order_consumer process
        # (near-real-time); the ARQ worker only runs the reclaim safety net.
        cron(reclaim_stale_order_intents, minute={i for i in range(60)}),
        cron(report_queue_depth, minute={i for i in range(60)}),
        # 每分鐘都要發(包含 0),否則預熱的告警會停在 INSUFFICIENT_DATA 而不是 OK。
        cron(publish_prewarm_signal, minute={i for i in range(60)}),
        cron(purge_old_audit_logs, hour={2}, minute={30}),
        cron(purge_old_stripe_events, hour={2}, minute={45}),
        cron(detect_inventory_drift, minute=set(range(0, 60, 5))),
        cron(detect_seat_structure_drift, minute=set(range(0, 60, 5))),
        cron(purge_finished_event_keys, hour={3}, minute={30}),

    ]

