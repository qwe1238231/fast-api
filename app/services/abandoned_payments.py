"""在途付款的超時(T2):收掉「建了 PaymentIntent 然後放棄」的訂單。

**為什麼需要這一支**:使用者按了付款、拿到 client_secret、關掉分頁。API 直接建的
PaymentIntent 在 Stripe 那邊**不會自動取消,也不發任何 webhook**;而這種訂單又被 T1
的 `expire_pending_orders` 刻意跳過(付款中的人不該被 10 分鐘超時抽掉票)。兩邊都不管
的結果是 PENDING 到永遠、座位鎖死。

**一條不變式,所有分支都從它推出來:本地 EXPIRED 之前,必須確知 Stripe 的 intent 是
`canceled`。** 反過來(先 expire 再 cancel)會開出一個窗:窗內付款成功,`succeeded`
進來面對終態訂單、走退款 —— 那條路存在,但它是兜底,不該被自己的 cron 當常規路徑踩。
所以每一筆的順序固定是:先問 Stripe → 只有 canceled 才動本地 → commit → 才還座位。

狀態轉移有兩個寫入者:這裡,和稍後到的 `payment_intent.canceled` webhook。**這是可以
的**:兩者都是 CAS、都冪等,誰先誰贏。「何時放棄」的決定只有一個(cutoff),webhook
只是把 Stripe 的狀態照回來,不是第二個決策者。

DB session 不跨越 Stripe 往返(規矩同 detect_seat_structure_drift):撈 id 用一個短
session 就關,每筆處理再開自己的短 session。一條 pooled 連線被抓著等 Stripe 回應,
會把 vacuum 的 xmin horizon 一起釘住。
"""
import asyncio
import logging
from dataclasses import dataclass
from datetime import datetime

import stripe
from redis.asyncio import Redis as RedisClient
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker
from stripe import StripeClient

from app.core.logging import alert, log_context
from app.models.order import Order, OrderStatus
from app.services.orders import expire_order, release_order_seat
from app.services.stripe_client import cancel_payment_intent

logger = logging.getLogger(__name__)

ABANDON_BATCH = 200
"""單次掃描最多處理幾筆。熱賣時被放棄的 intent 一分鐘可能幾百筆,而每筆是一次 Stripe
往返(STRIPE_TIMEOUT_SECONDS 上限 10 秒);沒有上限的話一輪可能佔住 worker 的一格
(max_jobs=4)好幾分鐘。撞到上限由呼叫端 alert,不能靜靜收工 —— 一個安靜的上限讀起來
就像「全部處理完了」。"""

ABANDON_CONCURRENCY = 3
"""同時在飛的 Stripe 呼叫數,**同時也是同時開著的 DB session 數**(semaphore 包住整筆
處理)。

3 = **部署的** worker 池子的 DB_POOL_SIZE(infra/taskdefs.tf 的 worker_pool_env,3+3),
不是 Settings 的本機預設 5 —— 預算看的永遠是 taskdef。最壞的一刻是這支滿載 3 條 +
其他 (max_jobs − 1) = 3 支 cron 各一條 = 6,剛好貼齊 pool + overflow;
test_deploy_pipeline 的預算測試釘住這個關係,想加併發要連池子一起加。
200 筆 / 3 併發 × 約 0.4 秒一次往返 ≈ 27 秒一輪,對每分鐘的 cron 仍有餘裕。"""


@dataclass(slots=True)
class AbandonedPaymentSweep:
    """一輪掃描的結果。**只回報事實**,告警與否由呼叫端決定 —— 這一層有每筆的 context
    (order_id / intent),適合記每筆的 log;每輪層級的判斷(撞 cap、錯誤率)是 job 的事。"""

    expired: int = 0
    """Stripe 確認 canceled,本地也 EXPIRED 並還了座位。"""
    awaiting_webhook: int = 0
    """Stripe 說 succeeded:錢到了、webhook 還沒到。不碰,留給 `succeeded` 去 CONFIRMED。"""
    in_flight: int = 0
    """processing / requires_* 之類,Stripe 拒絕取消。這輪不動,下輪再來。"""
    missing: int = 0
    """Stripe 說沒有這個 intent(resource_missing)。資料不一致,不是使用者放棄 —— alert,不自動 EXPIRED。"""
    errors: int = 0
    """網路錯、其他 Stripe 錯、或本地 DB 錯。這輪不動,下輪再來。"""
    capped: bool = False
    """候選比 batch 多。積壓正在長大,呼叫端要叫出來。"""


async def expire_abandoned_payments(
    *,
    session_factory: async_sessionmaker[AsyncSession],
    stripe_client: StripeClient,
    redis: RedisClient,
    cutoff: datetime,
    batch: int = ABANDON_BATCH,
    concurrency: int = ABANDON_CONCURRENCY,
) -> AbandonedPaymentSweep:
    """收掉 `created_at < cutoff`、PENDING、且已建 PaymentIntent 的訂單。

    候選集合走 ix_orders_paying_sweep(WHERE status='pending' AND payment_provider_id
    IS NOT NULL,range on created_at)。多撈一筆(batch + 1)只為了知道有沒有撞 cap,
    多的那筆不處理。ORDER BY created_at:撞 cap 時先收最老的,那些人等最久。
    """
    async with session_factory() as db:
        rows = (
            await db.execute(
                select(Order.id, Order.payment_provider_id)
                .where(
                    Order.status == OrderStatus.PENDING,
                    Order.payment_provider_id.is_not(None),
                    Order.created_at < cutoff,
                )
                .order_by(Order.created_at)
                .limit(batch + 1)
            )
        ).all()

    result = AbandonedPaymentSweep(capped=len(rows) > batch)
    gate = asyncio.Semaphore(concurrency)

    async def settle(order_id: int, intent_id: str) -> None:
        async with gate:
            # gather 把每個 coroutine 包成 Task,各自拿到 contextvars 的複本,所以這裡
            # 綁的欄位不會洩漏到隔壁那筆 —— 這是 log_context 在併發下仍然正確的前提。
            with log_context(order_id=order_id, payment_intent_id=intent_id):
                await _settle_one(
                    session_factory=session_factory,
                    stripe_client=stripe_client,
                    redis=redis,
                    order_id=order_id,
                    intent_id=intent_id,
                    result=result,
                )

    await asyncio.gather(*(settle(order_id, intent_id) for order_id, intent_id in rows[:batch]))
    return result


async def _settle_one(
    *,
    session_factory: async_sessionmaker[AsyncSession],
    stripe_client: StripeClient,
    redis: RedisClient,
    order_id: int,
    intent_id: str,
    result: AbandonedPaymentSweep,
) -> None:
    """一筆訂單:問 Stripe → 依狀態分支 → 只在 canceled 時動本地。

    所有例外都在這裡收掉,不往 gather 丟:一筆炸掉不該讓同批其他筆被取消。
    counters 在 await 之間沒有讀改寫,單執行緒 event loop 下不需要鎖。
    """
    try:
        status = await cancel_payment_intent(stripe_client, payment_intent_id=intent_id)
    except stripe.InvalidRequestError as exc:
        if exc.code == "resource_missing":
            # 訂單指著一個 Stripe 說不存在的 intent。這不是「使用者放棄」,是資料不一致
            # (寫錯 id?測試模式的 intent 進了正式 DB?),自動 EXPIRED 會把症狀擦掉、
            # 讓它永遠不被查。留在 PENDING 給人看;每分鐘會再 alert 一次,直到有人處理。
            alert(
                logger,
                "order points at a PaymentIntent Stripe does not know — leaving it "
                "PENDING; fix the row by hand",
                event="abandoned_intent_missing",
            )
            result.missing += 1
            return
        logger.warning(
            "stripe rejected the cancel; will retry next sweep",
            extra={"event": "abandoned_cancel_rejected", "stripe_code": exc.code},
            exc_info=True,
        )
        result.errors += 1
        return
    except Exception:
        # 網路層、逾時、Stripe 5xx。不知道 Stripe 那邊的狀態就不動本地 —— 下一輪再問。
        logger.warning(
            "stripe cancel failed; will retry next sweep",
            extra={"event": "abandoned_cancel_failed"},
            exc_info=True,
        )
        result.errors += 1
        return

    if status == "succeeded":
        # 錢已經到了,只是 succeeded webhook 還沒進來(或掉了)。**絕不能** EXPIRED。
        # INFO 而不是 WARNING:webhook 慢個幾秒是常態。但同一筆連續好幾輪都在這裡,
        # 就是 webhook 投遞壞了 —— 那個訊號就是這行每分鐘出現一次。
        logger.info(
            "charge already landed on this order; leaving it for the succeeded webhook",
            extra={"event": "abandoned_intent_succeeded"},
        )
        result.awaiting_webhook += 1
        return

    if status != "canceled":
        logger.debug(
            "intent still in flight; not expiring",
            extra={"event": "abandoned_intent_in_flight", "intent_status": status},
        )
        result.in_flight += 1
        return

    # Stripe 那邊確定不會再有錢進來了。現在才可以動本地。
    async with session_factory() as db:
        try:
            order = await db.get(Order, order_id)
            if order is None or order.status != OrderStatus.PENDING:
                # 撈 id 到現在之間有人先動了(canceled webhook 先到、或使用者按了取消)。
                # 座位是那個轉移的責任,這裡什麼都不做 —— 尤其不能 release。
                logger.debug(
                    "order left PENDING before we could expire it",
                    extra={"event": "abandoned_expire_raced"},
                )
                return
            if not await expire_order(db, order):
                return  # CAS 輸掉:同一瞬間有別的寫入者,同上
            await db.commit()
        except Exception:
            await db.rollback()
            logger.exception(
                "failed to expire abandoned order",
                extra={"event": "abandoned_expire_failed"},
            )
            result.errors += 1
            return

        result.expired += 1
        logger.info(
            "abandoned payment expired; seat returned",
            extra={"event": "abandoned_payment_expired"},
        )
        # post-commit、冪等。這裡失敗是「座位暫時沒回去」,不是「過期失敗」——
        # EXPIRED 的 CAS 同交易掛了 outbox 列,relay 會重試到還掉為止。
        try:
            await release_order_seat(db, redis, order)
        except Exception:
            logger.warning(
                "abandoned order expired but the fast-path seat release failed — "
                "the outbox relay will retry it",
                extra={"event": "seat_release_fast_path_failed"},
                exc_info=True,
            )
