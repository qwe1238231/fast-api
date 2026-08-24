"""Inventory service — Redis-backed atomic seat counter.

Single source of truth for "how many seats remain". DB stores `events.total_seats`
as configuration; this module manages live decrement during sale.
"""
import time

from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy import  select, func

from redis.asyncio import Redis
from redis.commands.core import AsyncScript

from enum import StrEnum
from dataclasses import dataclass

from app.core.config import get_settings
from app.core.exceptions import InsufficientInventory ,EventNotFound,InventoryNotReconcilable
from app.core.logging import current_trace_id
from app.models.event import Event
from app.models.order import Order, OrderStatus
from app.services.idempotency import _key as _claim_key
from app.services.queue_events import publish_event_poke

#: 訂單意圖的主串流。
#:
#: **這一條刻意沒有 MAXLEN,不要「補上」。** audit 與 dead-letter 都有上限,所以下一個
#: 讀到這裡的人很自然會覺得這裡漏了 —— 但這兩者的內容意義完全不同:
#:   - audit / dead-letter 裡的東西已經定案了,修剪掉最舊的只是丟掉歷史。
#:   - 這條裡的每一筆都是**已經扣過庫存、還沒寫進 DB** 的訂單。修剪 = 那個人付了
#:     admission、佔了位子、拿到 202,然後訂單人間蒸發,而庫存永遠回不來。
#: 它的有界性來自另一端:消費者落帳之後原子地 XACK + XDEL(worker.py 的 _ack_and_remove),
#: 所以正常情況下長度就是 backlog。消費者掛掉時它會漲 —— 那正是 ORDER_BACKLOG_WARN
#: 與斷路器要偵測的事,答案是把消費者修好,不是把訂單丟掉。
ORDER_STREAM_KEY="orders:stream"
ORDER_DEAD_LETTER_KEY="orders:stream:dead"

#: 死信串流的長度上限(近似修剪,跟 AUDIT_STREAM_MAX_LEN 同一個模式)。
#:
#: 沒有上限的話它只增不減:沒有人會去清它,而每一筆都留著完整的 order intent 欄位。
#: 一次持續的下游故障就能把 Redis 撐爆 —— 而 Redis 同時是庫存的唯一真相來源,
#: 所以「訂單寫不進 DB」會升級成「整個站賣不了票」。
#: 10 萬筆遠超過任何值得人工排查的量;真的到那個量級,問題不在保留了幾筆。
ORDER_DEAD_LETTER_MAX_LEN = 100_000



def _key(event_id: int) -> str:
    """Key for an event's available seat counter."""
    return f"event:{event_id}:available"

def _purchased_key(event_id: int) -> str:
    """每人限購的計數器:一個 event 一個 hash,field 是 user_id,值是持有張數。

    刻意用「一個 event 一個 hash」而不是 `purchased:{event}:{user}` 這種每人一把鍵:
    後者無法從 Postgres 枚舉,只能靠 TTL 回收,而 TTL 必須撐過整個銷售期 —— 猜短了
    額度會在銷售中途悄悄重置(限購形同虛設),猜長了就是永遠清不掉的垃圾。hash 讓
    `purge_finished_event_keys` 用既有的「從 DB 推導鍵集合」那套原樣處理。

    欄位數上限是這場次的**成交人數**(被限購擋掉的人不會寫入),所以不會爆。
    """
    return f"event:{event_id}:purchased"


def _released_key(marker: str) -> str:
    """Idempotency marker for a single release event (guards double-release)."""
    return f"released:{marker}"

async def queue_depth(redis: Redis) -> tuple[int, int]:
    """回傳 (backlog, dead_letter):尚未落帳的 order intents、與永久失敗的。
    兩者皆為 0 代表 DB 已追上 Redis,是『DB 此刻可信』的訊號。
    """
    backlog = await redis.xlen(ORDER_STREAM_KEY)
    dead = await redis.xlen(ORDER_DEAD_LETTER_KEY)
    return backlog, dead

async def set_initial_stock(
        redis: Redis,
        *,
        event_id: int,
        total_seats: int,
) -> None:
    """Initialize available counter when event launches. Idempotent (NX)."""
    await redis.set(_key(event_id), total_seats, nx=True)

async def get_available(
        redis: Redis,
        *,
        event_id: int,
) -> int:
    """Read current available count. Returns 0 if key missing or negative (race).

    熱路徑與等候室用這一支:把「鍵不見了」當成 0 是**安全的預設**(表現成售完,
    不會超賣)。要區分兩者的地方用 `read_available`。
    """
    val = await redis.get(_key(event_id))
    return max(0, int(val)) if val is not None else 0


async def read_available(
        redis: Redis,
        *,
        event_id: int,
) -> int | None:
    """讀庫存,**鍵不存在時回 None**。

    為什麼需要跟 `get_available` 分開:那一支把缺鍵折成 0,於是「賣完了」與「Redis 把
    庫存弄丟了」變成同一個值。前者是正常結局,後者是事故 —— 而事故的外觀會是「這場
    突然完售」,沒有任何一個計數器不一致,所以不會有任何訊號亮起來。

    對帳/偵測那一側必須看得到這個差別,才能決定是「重建」還是「告警」。
    """
    val = await redis.get(_key(event_id))
    return None if val is None else int(val)


async def oldest_intent_age_seconds(redis: Redis, *, now: float | None = None) -> float:
    """stream 裡最舊那一筆 order intent 的年紀(秒)。空的話回 0。

    **這個數字就是「Redis 現在死掉會丟掉多少」的量化值** —— 那些 intent 已經扣了庫存、
    已經回了 202,但還沒進 Postgres。backlog 的**筆數**回答「積了多少」,年紀回答
    「積了多久」,而後者才是耐久性的曝險窗:副本的非同步複寫只能保護最後幾百毫秒,
    這個數字告訴你實際暴露的是幾百毫秒還是十分鐘。

    不需要額外記帳:Redis 的 stream id 前半就是毫秒時間戳。
    """
    entries = await redis.xrange(ORDER_STREAM_KEY, count=1)
    if not entries:
        return 0.0
    added_ms = int(entries[0][0].split("-")[0])
    reference = time.time() if now is None else now
    return max(0.0, reference - added_ms / 1000)

# Atomic script: dedup + per-user cap + decrement stock + enqueue + write claim,
# all-or-nothing.
# KEYS[1]=stock key   KEYS[2]=claim key   KEYS[3]=stream key   KEYS[4]=purchased hash
# ARGV[1]=quantity  ARGV[2]=claim TTL seconds  ARGV[3]=user_id
# ARGV[4]=event_id  ARGV[5]=total_price_cents  ARGV[6]=idempotency_key
# ARGV[7]=zone_id ('' = 無座位圖的場次)  ARGV[8]=每人限購張數  ARGV[9]=trace_id
# Returns (a list on the Python side):
#   {'DUP'}                            -> this idempotency_key was already processed
#   {'SOLD_OUT', remaining}            -> not enough stock
#   {'OVER_LIMIT', already, limit}     -> this user already holds their cap
#   {'OK', stream msg id, remaining}   -> seat reserved and order intent enqueued
#                                         (remaining lets the caller detect a sold-out crossing)
_RESERVE_AND_ENQUEUE_LUA = """
local qty = tonumber(ARGV[1])

-- 1) Dedup: if the claim already exists, return DUP and never decrement.
if redis.call('EXISTS', KEYS[2]) == 1 then
    return {'DUP'}
end

-- 2) Read stock and check sold-out (GET on a missing key yields false -> nil after tonumber).
--    **順序重要:庫存先於限購。** 只剩 5 席卻要 6 張時回「你超過限購」是誤導 ——
--    使用者會以為減少張數就買得到。回 SOLD_OUT 並帶著 avail 才是可行動的。
--    反過來(有庫存但這個人買滿了)才輪到 OVER_LIMIT,那時它就是唯一正確的原因。
local avail = tonumber(redis.call('GET', KEYS[1]))
if avail == nil or avail < qty then
    return {'SOLD_OUT', tostring(avail or 0)}
end

-- 3) 每人限購。讀-判-寫全在這支腳本裡才有意義:分開做的話同一個人同時送 20 筆,
--    20 筆都會在任何一筆遞增之前讀到舊值而全部放行。Lua 是單執行緒且整支原子,
--    所以這裡的 HGET 與下面的 HINCRBY 之間不可能插進別的請求。
local limit = tonumber(ARGV[8])
local held = tonumber(redis.call('HGET', KEYS[4], ARGV[3])) or 0
if held + qty > limit then
    return {'OVER_LIMIT', tostring(held), tostring(limit)}
end

-- 4) Decrement stock and charge the buyer's quota. 兩者必須同進同出 —— 只扣其中
--    一個的話,不是有人多買就是額度憑空消失。
redis.call('DECRBY', KEYS[1], qty)
redis.call('HINCRBY', KEYS[4], ARGV[3], qty)

-- 5) Push the order intent into the stream, capture the auto-generated message id.
--    zone_id 傳 '' 代表無座位圖的場次(worker 會還原成 NULL)。Redis stream 的
--    欄位值只能是字串,沒有 nil,所以用空字串當哨兵。
--    trace_id 是**跨 process 的手動搬運**:contextvar 只活在 API 的進程裡,
--    消費者拿不到,所以它必須跟著訊息本身走(等同 HTTP 的 traceparent header)。
--    有了它,「下單 → 落帳 → reclaim → 死信」在 log 裡是同一個 id 串起來的一條線。
local stream_id = redis.call('XADD', KEYS[3], '*',
    'user_id', ARGV[3],
    'event_id', ARGV[4],
    'quantity', ARGV[1],
    'total_price_cents', ARGV[5],
    'idempotency_key', ARGV[6],
    'zone_id', ARGV[7],
    'trace_id', ARGV[9])

-- 6) Write the claim (with TTL) so the next request with the same key is blocked at step 1.
redis.call('SET', KEYS[2], 'PENDING', 'EX', tonumber(ARGV[2]))

return {'OK', stream_id, tostring(avail - qty)}
"""

# Registered once on first use (SHA1 computed there, not per order), then reused.
# Constructing it needs a live client for the encoder, so it can't be built at import.
_reserve_script: AsyncScript | None = None


async def reserve(
        redis: Redis,
        *,
        event_id: int,
        quantity: int,
) -> None:
    """Atomically reserve `quantity` seats. Raises InsufficientInventory on failure."""
    remaining = await redis.decrby(_key(event_id), quantity)
    if remaining < 0 :
        await redis.incrby(_key(event_id), quantity)
        available =remaining + quantity
        raise InsufficientInventory(
            event_id=event_id,
            requested=quantity,
            available=available,
        )
    if remaining == 0:                                   # just took the last seat(s)
        await publish_event_poke(redis, event_id)        # wake waiters -> they re-read -> sold_out

# Atomic idempotent release: mark-then-return-seats-and-quota, all-or-nothing.
# KEYS[1]=released marker   KEYS[2]=stock key   KEYS[3]=purchased hash
# ARGV[1]=quantity          ARGV[2]=marker TTL seconds   ARGV[3]=user_id
# The SETNX marker makes a replayed release (crash between commit and release,
# stream re-delivery, double cancel) a no-op — seats are returned at most once.
# 額度退回放在同一個 marker 底下,所以它跟座位一樣「最多退一次」。分開做的話重放會
# 把額度退成負的,那個人就能無限買。
# Returns (a tagged list, like the reserve script):
#   {'OK', new_available}  -> first release; seats returned
#   {'DUP'}                -> already released; no-op
# A tagged table (not a bare -1 sentinel) so a legitimate new count of -1 — reachable
# when reconcile writes negative stock during oversell recovery — isn't misread as DUP.
_RELEASE_LUA = """
if redis.call('SET', KEYS[1], '1', 'NX', 'EX', tonumber(ARGV[2])) then
    -- 退額度並在歸零時刪掉欄位:既是垃圾回收,也是**負值的地板**。這條路徑會遇到
    -- 功能上線前建立的舊訂單 —— 它們從來沒有加過額度,直接 HINCRBY 會把欄位打成
    -- 負數,那個人就憑空多了額度。
    local left = redis.call('HINCRBY', KEYS[3], ARGV[3], -tonumber(ARGV[1]))
    if left <= 0 then
        redis.call('HDEL', KEYS[3], ARGV[3])
    end
    return {'OK', tostring(redis.call('INCRBY', KEYS[2], tonumber(ARGV[1])))}
end
return {'DUP'}
"""

# Registered once on first use (same pattern as the reserve script).
_release_script: AsyncScript | None = None


async def release(
        redis: Redis,
        *,
        event_id: int,
        user_id: int,
        quantity: int,
        marker: str,
        ttl_seconds: int = 86400,
) -> bool:
    """Return `quantity` seats to inventory **and to the buyer's quota** — idempotently.

    `marker` uniquely identifies THIS release event; the `released:{marker}`
    SETNX guard returns the seats at most once even if the call is replayed
    (a crash between commit and release, a reclaimed stream entry, a double
    cancel). Use "order:{id}" for expire/cancel and "dl:{idempotency_key}" for
    dead-letter. Returns True if this call actually returned the seats, False
    if it was a no-op because they were already released.

    `user_id` 沒有預設值是刻意的:限購含 PENDING,所以**每一條**釋放路徑都必須退
    額度。漏掉任何一條的症狀是「下單失敗過的人再也買不了這場」,而那要等到客訴才會
    被發現。做成必填讓漏掉在型別檢查時就炸,而不是在生產環境安靜地少退。
    """
    global _release_script
    if _release_script is None:
        _release_script = redis.register_script(_RELEASE_LUA)   # SHA1 once
    result = await _release_script(
        keys=[_released_key(marker), _key(event_id), _purchased_key(event_id)],
        args=[quantity, ttl_seconds, user_id],
        client=redis,
    )
    if result[0] == "DUP":
        return False                                     # replayed release — no-op, no crossing
    new_available = int(result[1])
    if new_available - quantity <= 0 < new_available:    # crossed from sold-out back to available
        await publish_event_poke(redis, event_id)        # wake waiters -> they re-read -> not sold_out
    return True

async def reconcile_inventory(
        db: AsyncSession,
        redis: Redis,
        *,
        event_id: int,
        force: bool = False,
) -> int:
    """從 Postgres 重算真實剩餘,覆蓋寫回 Redis。Redis 遺失後的權威重建。"""
    if not force:                                    # ← 守衛搬進來
        backlog, dead = await queue_depth(redis)
        # Only un-persisted backlog means inventory is genuinely in flight and the
        # DB SUM can't be trusted. dead-lettered intents are already settled
        # (batch 1: persisted → counted in the SUM; not persisted → seat refunded),
        # so dead>0 must NOT block reconcile — else one poison disables it forever.
        if backlog:
            raise InventoryNotReconcilable(event_id, backlog, dead)
    total_seats = await db.scalar(
        select(Event.total_seats).where(Event.id == event_id)
    )
    if total_seats is None:
        raise EventNotFound(event_id=event_id)
    
    held = (OrderStatus.PENDING, OrderStatus.PAID, OrderStatus.CONFIRMED)
    sold = await db.scalar(
        select(func.coalesce(func.sum(Order.quantity), 0))
        .where(Order.event_id == event_id, Order.status.in_(held))
    )

    remaining = total_seats - sold
    await redis.set(_key(event_id), remaining)
    # 限購計數器也要一起重建。以前只重建 `available`,於是 Redis 遺失之後**每個人的
    # 限購額度都歸零** —— 已經買滿 4 張的人可以再買 4 張,而庫存看起來是對的(它剛被
    # 重建過),所以沒有任何訊號會亮。這比庫存漂移更難發現:超賣會撞到總量,超買不會。
    #
    # 兩者必須從**同一次** DB 讀出來的事實推導,而且共用同一道 backlog 守衛 —— 不然
    # 就會出現「庫存按新事實重建、額度按舊事實」這種內部不一致。
    await _rebuild_purchase_quotas(db, redis, event_id=event_id, held=held)
    return remaining


async def _rebuild_purchase_quotas(
        db: AsyncSession,
        redis: Redis,
        *,
        event_id: int,
        held: tuple[OrderStatus, ...],
) -> int:
    """把 `event:{e}:purchased` 覆寫成 Postgres 的事實。回傳有持有記錄的人數。

    用 DEL + HSET 而不是逐欄位修正:目標是「讓 Redis 等於 DB」,而多出來的欄位(某個
    人的訂單全被取消了,但退額度那一步沒跑到)只有整體覆寫才清得掉。逐欄位比對會把
    那些幽靈欄位永久留下,而它們的效果正是「那個人再也買不了這場」。
    """
    rows = (
        await db.execute(
            select(Order.user_id, func.sum(Order.quantity))
            .where(Order.event_id == event_id, Order.status.in_(held))
            .group_by(Order.user_id)
        )
    ).all()
    key = _purchased_key(event_id)
    async with redis.pipeline(transaction=True) as pipe:
        pipe.delete(key)
        if rows:
            pipe.hset(key, mapping={str(uid): int(qty) for uid, qty in rows})
        await pipe.execute()
    return len(rows)


async def compute_expected_quotas(
        db: AsyncSession,
        *,
        event_id: int,
) -> dict[str, int]:
    """從 Postgres 算出『每個人應該持有幾張』—— 不碰 Redis,純讀。給漂移偵測用。"""
    held = (OrderStatus.PENDING, OrderStatus.PAID, OrderStatus.CONFIRMED)
    rows = (
        await db.execute(
            select(Order.user_id, func.sum(Order.quantity))
            .where(Order.event_id == event_id, Order.status.in_(held))
            .group_by(Order.user_id)
        )
    ).all()
    return {str(uid): int(qty) for uid, qty in rows}


async def read_purchase_quotas(redis: Redis, *, event_id: int) -> dict[str, int]:
    """讀出 Redis 裡的限購持有量。"""
    raw = await redis.hgetall(_purchased_key(event_id))
    return {k: int(v) for k, v in raw.items()}

async def compute_expected_available(
        db: AsyncSession,
        *,
        event_id: int,
) -> int:
    """從 Postgres 算出『應有的剩餘庫存』—— 不碰 Redis,純讀。"""
    total_seats = await db.scalar(
        select(Event.total_seats).where(Event.id == event_id)
    )
    if total_seats is None:
        raise EventNotFound(event_id=event_id)
    
    held = (OrderStatus.PENDING, OrderStatus.PAID, OrderStatus.CONFIRMED)
    sold = await db.scalar(
        select(func.coalesce(func.sum(Order.quantity), 0))
        .where(Order.event_id == event_id, Order.status.in_(held))
    )
    return total_seats - sold

class ReserveOutcome(StrEnum):
    """The possible outcomes of reserve_and_enqueue."""
    OK = "OK"
    DUP = "DUP"
    SOLD_OUT = "SOLD_OUT"
    OVER_LIMIT = "OVER_LIMIT"       # 這個人在這場次已達限購 —— 不是賣完


@dataclass(frozen=True)
class ReserveResult:
    outcome: ReserveOutcome
    stream_id: str | None = None   # set only when outcome == OK
    available: int | None = None   # set only when outcome == SOLD_OUT
    held: int | None = None        # set only when outcome == OVER_LIMIT
    limit: int | None = None       # set only when outcome == OVER_LIMIT


async def reserve_and_enqueue(
        redis: Redis,
        *,
        event_id: int,
        user_id: int,
        quantity: int,
        total_price_cents: int,
        idempotency_key: str,
        zone_id: int | None = None,
        claim_ttl_seconds: int = 86400,
        max_per_user: int | None = None,
) -> ReserveResult:
    """Atomically: dedup + per-user cap + decrement stock + enqueue + write claim.

    Everything runs inside one Lua script, so no other request can interleave --
    that is what welds "decrement stock" and "enqueue" into a single action and
    closes the gap. 限購也必須在裡面:在外面先查再扣的話,同一個人同時送的請求會
    全部讀到同一個舊值而一起放行。
    """
    global _reserve_script
    if _reserve_script is None:
        _reserve_script = redis.register_script(_RESERVE_AND_ENQUEUE_LUA)   # SHA1 once
    if max_per_user is None:
        max_per_user = get_settings().MAX_TICKETS_PER_USER_PER_EVENT
    result = await _reserve_script(
        keys=[
            _key(event_id),                 # KEYS[1] stock
            _claim_key(idempotency_key),    # KEYS[2] claim
            ORDER_STREAM_KEY,               # KEYS[3] stream
            _purchased_key(event_id),       # KEYS[4] per-user quota hash
        ],
        args=[
            quantity,                       # ARGV[1]
            claim_ttl_seconds,              # ARGV[2]
            user_id,                        # ARGV[3]
            event_id,                       # ARGV[4]
            total_price_cents,              # ARGV[5]
            idempotency_key,                # ARGV[6]
            "" if zone_id is None else zone_id,   # ARGV[7]
            max_per_user,                   # ARGV[8]
            # 沒有綁定 context 時是空字串(worker 直接呼叫、測試),消費者那邊
            # 會退回 "-";不要傳 None,Redis 的欄位值只能是字串。
            current_trace_id() or "",       # ARGV[9]
        ],
        client=redis,                       # current client (app or test)
    )

    # client has decode_responses=True, so result items are already str (not bytes).
    status = result[0]
    if status == ReserveOutcome.DUP:
        return ReserveResult(outcome=ReserveOutcome.DUP)
    if status == ReserveOutcome.SOLD_OUT:
        return ReserveResult(
            outcome=ReserveOutcome.SOLD_OUT,
            available=int(result[1]),
        )
    if status == ReserveOutcome.OVER_LIMIT:
        return ReserveResult(
            outcome=ReserveOutcome.OVER_LIMIT,
            held=int(result[1]),
            limit=int(result[2]),
        )
    remaining = int(result[2])
    if remaining == 0:                                   # this reservation took the last seat(s)
        await publish_event_poke(redis, event_id)        # wake waiters -> they re-read -> sold_out
    return ReserveResult(
        outcome=ReserveOutcome.OK,
        stream_id=result[1],
    )
    