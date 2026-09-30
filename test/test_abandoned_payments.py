"""`expire_abandoned_payments`:T2 超時掃描。

每一條測的都是同一條不變式的一個面:**只有 Stripe 說 canceled,本地才會 EXPIRED 並還
座位**。其他每一種 Stripe 回應都必須讓訂單原地不動。

Stripe client 是 MagicMock;DB 與 Redis 是真的(conftest 的 fixture),因為這支的
價值正是在「Stripe 的答案 → 本地狀態 + 庫存」這條線上。
"""
from datetime import datetime, timedelta, timezone
from unittest.mock import AsyncMock, MagicMock
from uuid import uuid4

import pytest
import stripe
from sqlalchemy import select

from app.core.config import get_settings
from app.db.session import AsyncSessionLocal
from app.models.order import Order, OrderStatus
from app.models.user import User
from app.services.abandoned_payments import AbandonedPaymentSweep, expire_abandoned_payments
from app.services.inventory import get_available, reserve
from app.services.orders import cancel_order
from app.worker import WorkerSettings, shutdown, startup
from app.worker import expire_abandoned_payments as sweep_job

T2 = timedelta(minutes=15)
PAST_T2 = T2 + timedelta(minutes=1)
WITHIN_T2 = timedelta(minutes=5)


def _cutoff() -> datetime:
    return datetime.now(timezone.utc) - T2


async def _order(db, redis, event, *, age: timedelta, intent: str | None = "pi_abandoned") -> Order:
    """一筆 PENDING 訂單,佔一張票。`intent=None` 代表還沒開始付款(那是 T1 的客人)。"""
    user = User(username=f"u-{uuid4().hex[:8]}", hashed_password="x")
    db.add(user)
    await db.flush()
    await reserve(redis, event_id=event.id, quantity=1)
    order = Order(
        user_id=user.id, event_id=event.id, quantity=1, total_price_cents=1500,
        idempotency_key=uuid4(), status=OrderStatus.PENDING,
        created_at=datetime.now(timezone.utc) - age, payment_provider_id=intent,
    )
    db.add(order)
    await db.commit()
    return order


def _unexpected_state(status: str) -> stripe.InvalidRequestError:
    return stripe.InvalidRequestError(
        "cannot cancel", "intent", code="payment_intent_unexpected_state",
        json_body={"error": {
            "code": "payment_intent_unexpected_state",
            "payment_intent": {"id": "pi_abandoned", "status": status},
        }},
    )


def _stripe_saying(status: str = "canceled", *, error: Exception | None = None) -> MagicMock:
    """一個對每次 cancel 都回同一個答案的 Stripe。`canceled` 是正常回傳,其他狀態走
    unexpected_state 的錯誤體(那是 Stripe 真實的行為)。"""
    client = MagicMock()
    if error is not None:
        cancel = AsyncMock(side_effect=error)
    elif status == "canceled":
        cancel = AsyncMock(return_value=MagicMock(status="canceled"))
    else:
        cancel = AsyncMock(side_effect=_unexpected_state(status))
    client.v1.payment_intents.cancel_async = cancel
    return client


async def _sweep(stripe_client: MagicMock, redis, **overrides):
    return await expire_abandoned_payments(
        session_factory=AsyncSessionLocal, stripe_client=stripe_client, redis=redis,
        cutoff=_cutoff(), **overrides,
    )


@pytest.mark.asyncio
async def test_canceled_intent_expires_the_order_and_returns_the_seat(db, redis, published_event):
    order = await _order(db, redis, published_event, age=PAST_T2)
    before = await get_available(redis, event_id=published_event.id)
    client = _stripe_saying("canceled")

    result = await _sweep(client, redis)

    await db.refresh(order)
    assert order.status == OrderStatus.EXPIRED
    assert await get_available(redis, event_id=published_event.id) == before + 1
    assert result.expired == 1
    client.v1.payment_intents.cancel_async.assert_awaited_once_with(
        "pi_abandoned", {"cancellation_reason": "abandoned"}
    )


@pytest.mark.parametrize("stripe_status", ["succeeded", "processing", "requires_action"])
@pytest.mark.asyncio
async def test_any_other_stripe_status_leaves_the_order_alone(db, redis, published_event, stripe_status):
    """不變式的核心:Stripe 沒說 canceled,本地一個字都不動、一張票都不還。

    `succeeded` 尤其關鍵 —— 那是錢已經進來、webhook 還在路上。這裡 EXPIRED 的話,
    webhook 到了會走退款,使用者付了錢、被退款、票沒了。
    """
    order = await _order(db, redis, published_event, age=PAST_T2)
    before = await get_available(redis, event_id=published_event.id)

    result = await _sweep(_stripe_saying(stripe_status), redis)

    await db.refresh(order)
    assert order.status == OrderStatus.PENDING
    assert await get_available(redis, event_id=published_event.id) == before
    assert result.expired == 0
    assert (result.awaiting_webhook, result.in_flight) == (
        (1, 0) if stripe_status == "succeeded" else (0, 1)
    )


@pytest.mark.asyncio
async def test_missing_intent_is_reported_not_expired(db, redis, published_event):
    """Stripe 不認識這個 intent 是資料不一致,不是使用者放棄。自動 EXPIRED 會把症狀擦掉。"""
    order = await _order(db, redis, published_event, age=PAST_T2)
    missing = stripe.InvalidRequestError("No such payment_intent", "intent", code="resource_missing")

    result = await _sweep(_stripe_saying(error=missing), redis)

    await db.refresh(order)
    assert order.status == OrderStatus.PENDING
    assert (result.missing, result.expired) == (1, 0)


@pytest.mark.asyncio
async def test_transport_error_is_counted_and_retried_next_round(db, redis, published_event):
    """不知道 Stripe 那邊的狀態就不動本地。錯誤只計數,訂單留給下一輪。"""
    order = await _order(db, redis, published_event, age=PAST_T2)

    result = await _sweep(_stripe_saying(error=stripe.APIConnectionError("boom")), redis)

    await db.refresh(order)
    assert order.status == OrderStatus.PENDING
    assert (result.errors, result.expired) == (1, 0)


@pytest.mark.asyncio
async def test_orders_within_t2_are_not_even_asked_about(db, redis, published_event):
    """還在 T2 內的人正在填卡號。連 Stripe 都不該問 —— 問了也不會動,但那是浪費的往返。"""
    await _order(db, redis, published_event, age=WITHIN_T2)
    client = _stripe_saying("canceled")

    result = await _sweep(client, redis)

    client.v1.payment_intents.cancel_async.assert_not_awaited()
    assert result.expired == 0


@pytest.mark.asyncio
async def test_orders_without_an_intent_belong_to_t1(db, redis, published_event):
    """沒有 payment_provider_id 的訂單是 expire_pending_orders 的客人,這裡不碰。
    兩支 cron 的集合互斥,靠的就是 IS NULL / IS NOT NULL 這一刀。"""
    await _order(db, redis, published_event, age=PAST_T2, intent=None)
    client = _stripe_saying("canceled")

    result = await _sweep(client, redis)

    client.v1.payment_intents.cancel_async.assert_not_awaited()
    assert result.expired == 0


@pytest.mark.asyncio
async def test_batch_cap_is_reported_and_oldest_go_first(db, redis, published_event):
    """候選比 batch 多 → capped=True,而且先收最老的那筆。"""
    oldest = await _order(db, redis, published_event, age=PAST_T2 + timedelta(minutes=30), intent="pi_oldest")
    newer = await _order(db, redis, published_event, age=PAST_T2, intent="pi_newer")
    client = _stripe_saying("canceled")

    result = await _sweep(client, redis, batch=1)

    assert result.capped is True
    assert result.expired == 1
    client.v1.payment_intents.cancel_async.assert_awaited_once()
    await db.refresh(oldest)
    await db.refresh(newer)
    assert (oldest.status, newer.status) == (OrderStatus.EXPIRED, OrderStatus.PENDING)


@pytest.mark.asyncio
async def test_losing_the_race_means_no_release(db, redis, published_event):
    """撈 id 之後、CAS 之前,使用者按了取消。座位是那個轉移的責任 —— 這裡**不能**再還
    一次,否則同一張票回到市場兩次,就是超賣。

    用 Stripe 呼叫當鉤子:cancel 回應的那一刻在另一個 session 把訂單 CAS 成 CANCELLED。
    """
    order = await _order(db, redis, published_event, age=PAST_T2)
    before = await get_available(redis, event_id=published_event.id)

    async def cancel_then_lose_the_race(*_args, **_kwargs) -> MagicMock:
        async with AsyncSessionLocal() as other:
            victim = await other.get(Order, order.id)
            assert await cancel_order(other, victim)
            await other.commit()
        return MagicMock(status="canceled")

    client = MagicMock()
    client.v1.payment_intents.cancel_async = AsyncMock(side_effect=cancel_then_lose_the_race)

    result = await _sweep(client, redis)

    status_now = await db.scalar(select(Order.status).where(Order.id == order.id))
    assert status_now == OrderStatus.CANCELLED          # 不是 EXPIRED
    assert await get_available(redis, event_id=published_event.id) == before   # 沒有多還
    assert result.expired == 0


# ---------- job 層:worker.py 那支薄 cron ----------
#
# service 層已經測完「Stripe 的答案 → 本地狀態」。這裡只測 job 該做的三件事:餵對的
# client 和 cutoff、對每輪結果做對的判斷(撞 cap 要叫)、以及真的排在 cron 表上。
# service 本體 monkeypatch 掉 —— 不然這一層的每條測試都得再建一次訂單。

SWEEP_PATH = "app.services.abandoned_payments.expire_abandoned_payments"


def _events(caplog) -> set[str]:
    return {getattr(rec, "event", None) for rec in caplog.records}


@pytest.mark.asyncio
async def test_job_feeds_the_ctx_clients_and_a_t2_cutoff(redis, monkeypatch):
    """cutoff = now − PAYMENT_ABANDON_TIMEOUT_MINUTES,client 來自 ctx(startup 放的)。"""
    seen: dict = {}

    async def fake_sweep(**kwargs) -> AbandonedPaymentSweep:
        seen.update(kwargs)
        return AbandonedPaymentSweep()

    monkeypatch.setattr(SWEEP_PATH, fake_sweep)
    stripe_client = object()
    before = datetime.now(timezone.utc)

    await sweep_job({"redis_client": redis, "stripe_client": stripe_client})

    assert seen["stripe_client"] is stripe_client
    assert seen["redis"] is redis
    assert seen["session_factory"] is AsyncSessionLocal
    expected = before - timedelta(minutes=get_settings().PAYMENT_ABANDON_TIMEOUT_MINUTES)
    assert abs((seen["cutoff"] - expected).total_seconds()) < 5


@pytest.mark.parametrize(
    "sweep, expected_event",
    [
        pytest.param(AbandonedPaymentSweep(), None, id="quiet-minute"),
        pytest.param(AbandonedPaymentSweep(expired=1, in_flight=2), "abandoned_sweep", id="activity"),
        pytest.param(AbandonedPaymentSweep(expired=3, capped=True), "abandoned_sweep_capped", id="capped"),
    ],
)
@pytest.mark.asyncio
async def test_job_logs_exactly_what_the_round_deserves(redis, monkeypatch, caplog, sweep, expected_event):
    """全零的分鐘一行都不記(噪音);有動靜記 INFO;撞 cap 是 ALERT。三種要互斥。"""
    monkeypatch.setattr(SWEEP_PATH, AsyncMock(return_value=sweep))

    with caplog.at_level("DEBUG", logger="app.worker"):
        await sweep_job({"redis_client": redis, "stripe_client": object()})

    fired = _events(caplog) & {"abandoned_sweep", "abandoned_sweep_capped"}
    assert fired == ({expected_event} if expected_event else set())


@pytest.mark.asyncio
async def test_capped_is_a_needs_human_alert_carrying_the_counts(redis, monkeypatch, caplog):
    """撞 cap 走 alert():帶 needs_human 才會被 CloudWatch 的過濾器接到;帶計數才知道
    是「200 筆全過期」還是「200 筆全在 processing」—— 兩者要人做的事完全不同。"""
    monkeypatch.setattr(
        SWEEP_PATH, AsyncMock(return_value=AbandonedPaymentSweep(expired=150, in_flight=50, capped=True))
    )

    with caplog.at_level("ERROR", logger="app.worker"):
        await sweep_job({"redis_client": redis, "stripe_client": object()})

    [rec] = [r for r in caplog.records if getattr(r, "event", None) == "abandoned_sweep_capped"]
    assert rec.needs_human is True
    assert (rec.expired, rec.in_flight) == (150, 50)


def test_the_sweep_is_on_the_cron_table_every_minute_beside_t1():
    """兩道超時都每分鐘跑。T2 漏排的話,這整組修正就只剩「payment_failed 不再釋放座位」
    那一半 —— 那是純損失(見 payment_failed 的討論)。"""
    by_name = {cj.coroutine.__name__: cj for cj in WorkerSettings.cron_jobs}

    assert by_name["expire_abandoned_payments"].minute == set(range(60))
    assert by_name["expire_pending_orders"].minute == set(range(60))


@pytest.mark.asyncio
async def test_startup_wires_a_stripe_client_and_shutdown_closes_its_http(monkeypatch):
    """StripeClient 沒有 close,所以 startup 要把 HTTPXClient 也留在 ctx,shutdown 才關得掉。
    漏掉的話每次 worker 重啟都留一組 httpx 連線池不關 —— 本機看不出來,ECS 上是慢慢漏。"""
    http = MagicMock()
    http.close_async = AsyncMock()
    stripe_client = object()
    monkeypatch.setattr("app.worker.create_stripe_client", lambda _key: (stripe_client, http))
    fake_redis = MagicMock()
    fake_redis.xgroup_create = AsyncMock()
    fake_redis.aclose = AsyncMock()
    monkeypatch.setattr("app.worker.create_redis_client", lambda _url: fake_redis)
    ctx: dict = {}

    await startup(ctx)
    assert ctx["stripe_client"] is stripe_client
    assert ctx["stripe_http"] is http

    await shutdown(ctx)
    http.close_async.assert_awaited_once()
    fake_redis.aclose.assert_awaited_once()
