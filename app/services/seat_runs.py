"""Redis 上的 free-run 結構 —— 座位配位的熱路徑狀態。

架構決定(有量測依據,見 app/scripts/simulate_cas_contention.py):配位**不**移植
進 Lua。應用層讀出空段、用 app.services.seating 的純函式算出位置,再由一支很短
的 Lua 做 CAS(「這個空段還是原樣嗎?是就切下去」)。

    讀 runs (1 趟) → 在 Python 算 (0.08ms) → CAS Lua (1 趟)

理由是 N = QPS × 時間窗。`legal_anchors` 只花 0.08ms,時間窗約 1ms,所以在預設
放行速率(500/秒)下同時看到同一份快照的請求數 N ≈ 0.5 —— 幾乎不會互撞。閉環
模擬要到每個 zone 每秒 2000 筆、且不做隨機化,才會出現重試放大。代價是省下整個
Lua 移植與差異測試,而且只有一份實作不會漂移。

保險是 `TOP_K` 隨機化:不取最佳而是從前 K 個候選隨機挑。實測品質只差 0.117 個
座位、裝箱完全沒有代價,但把 λ=2000 的放大從 3.08× 壓回 1.03×。

**這個決定的支點是時間窗 T,而 T 是估的不是量的。** N 隨 T 線性成長:T=10ms 時
λ=500 就給出 N=5、K=1 只剩 20% 成功率。所以 CAS 失敗率與 T 的 p99 必須進監控,
越線就是該把配位搬進 Lua 的訊號 —— 屆時 seating.py 的純函式就是現成的 oracle。

Key schema(每個 zone 一組,zone 是分片單位):

    event:{e}:zone:{z}:runs   HASH  "{block}:{start}" → length   正向:空段
    event:{e}:zone:{z}:ends   HASH  "{block}:{end}"   → start    反向:結尾索引
    event:{e}:zone:{z}:geom   HASH  "{block}" → "cap:base:edge"  座位品質幾何
    event:{e}:zone:{z}:available  STRING  該區剩餘席數

`ends` 是關鍵訣竅:釋放時要問「左邊有沒有緊鄰的空段」,而那個空段的 **end** 等於
我的 start,hash 只能用 start 當 key 所以查不到。多一份 end→start 的反向索引,
左右鄰合併都變 O(1)。這是 memory allocator 的 boundary tags。
"""
import random
from dataclasses import dataclass

from redis.asyncio import Redis
from redis.commands.core import AsyncScript
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.exceptions import NoSeatsAvailable, SeatContention
from app.models.seating import Seat, SeatBlock, SeatHold
from app.services.idempotency import _key as _claim_key
from app.services.inventory import ORDER_STREAM_KEY, _key as _event_available_key
from app.services.queue_events import publish_event_poke
from app.services.seating import (
    ENDGAME_POLICY,
    NORMAL_POLICY,
    BlockGeometry,
    Placement,
    Policy,
    Run,
    legal_anchors,
)

#: 從前 K 個候選隨機挑,壓低 CAS 衝突。K=1 等於純決定性(高併發下會 thundering
#: herd);K=16 實測把 λ=2000 的重試放大從 3.08× 壓到 1.03×,品質只差 0.117 座。
TOP_K = 16

#: CAS 重試上限。超過就當成暫時性失敗往上拋 —— 有界重試,不要在熱路徑上無限迴圈。
MAX_CAS_ATTEMPTS = 5

#: 單筆訂單張數上限。必須與 OrderCreate.quantity 的 le= 一致。
MAX_TICKETS_PER_ORDER = 10

#: 連續幾筆被嚴格策略拒絕之後,這個 zone 就切換到收尾期策略(允許在邊緣留下孤兒)。
#:
#: 這是**偵測器不是旋鈕**:實測 3 / 10 / 30 三個數量級的結果完全相同,因為它偵測的
#: 是「結構性卡住」這個二元狀態 —— 只要還有任何訂單配得出來,計數器就被歸零。
#: 對照之下「剩餘率低於 X% 就放寬」的門檻依賴需求分布:0.05 對兩人票永遠不觸發
#: (銷售卡在 6% 就停了),三人票要 0.20 才夠 —— 而那是事前不知道的東西。
#:
#: 取 3 是因為切換前那幾筆會吃到 409,越小越好(達標的那一筆會在同一個請求內
#: 立刻用放寬策略重試,所以實際上只有 2 個人看到拒絕)。
STRICT_MISS_THRESHOLD = 3


def _runs_key(event_id: int, zone_id: int) -> str:
    return f"event:{event_id}:zone:{zone_id}:runs"


def _ends_key(event_id: int, zone_id: int) -> str:
    return f"event:{event_id}:zone:{zone_id}:ends"


def _geom_key(event_id: int, zone_id: int) -> str:
    return f"event:{event_id}:zone:{zone_id}:geom"


def _zone_available_key(event_id: int, zone_id: int) -> str:
    return f"event:{event_id}:zone:{zone_id}:available"


def _misses_key(event_id: int, zone_id: int) -> str:
    return f"event:{event_id}:zone:{zone_id}:strict_misses"


def _relaxed_key(event_id: int, zone_id: int) -> str:
    return f"event:{event_id}:zone:{zone_id}:relaxed"


def _released_key(marker: str) -> str:
    return f"released:{marker}"


async def is_relaxed(redis: Redis, *, event_id: int, zone_id: int) -> bool:
    """這個 zone 是否已經切進收尾期策略。單向 —— 一旦切了就不回頭。"""
    return bool(await redis.exists(_relaxed_key(event_id, zone_id)))


async def _record_strict_miss(redis: Redis, *, event_id: int, zone_id: int) -> bool:
    """記一次「嚴格策略配不出來」。回傳 True 表示該切到收尾期了。

    不做原子化:INCR 與 SET 之間的競態最多讓兩個請求同時把 ratchet 打開,而 SET
    本來就是冪等的。為了一個單向的布林旗標開 Lua 不划算。
    """
    misses = await redis.incr(_misses_key(event_id, zone_id))
    if misses < STRICT_MISS_THRESHOLD:
        return False
    await redis.set(_relaxed_key(event_id, zone_id), "1")
    return True


@dataclass(frozen=True, slots=True)
class ZoneState:
    """一個 zone 當下的空段與幾何 —— 配位與座位圖唯讀路徑共用的輸入。"""

    runs: list[Run]
    geometry: dict[int, BlockGeometry]

    @property
    def remaining(self) -> int:
        return sum(run.length for run in self.runs)


# ─────────────────────────────── 重建（權威復原路徑）

async def rebuild_zone_runs(
    db: AsyncSession, redis: Redis, *, event_id: int, zone_id: int
) -> int:
    """從 Postgres 重算整個 zone 的空段結構並覆寫 Redis。回傳剩餘席數。

    這是 Redis 遺失、或偵測到結構損壞(seat_holds 的 EXCLUDE 拒絕了一筆 worker
    落帳)之後唯一安全的復原方式。**不要試圖「退還」那筆被拒的區間** —— EXCLUDE
    拒絕代表區間與別人的 hold 重疊,而你無法從被拒的那筆判斷哪一段是誰的;盲目
    退還會把別人持有的座位標回可賣,把「DB 拒絕一筆」升級成真的兩人同座。

    重建則自動涵蓋了「該退的座位」:DB 沒有紀錄的座位就是空的。

    呼叫端必須先確認 order stream 已排空(見 inventory.queue_depth) —— in-flight
    的 intent 持有 Redis 認定的區間但 DB 還沒有,此時重建會把它們算成空的,
    worker 落帳時必然衝突。
    """
    blocks = (
        await db.scalars(select(SeatBlock).where(SeatBlock.zone_id == zone_id))
    ).all()
    if not blocks:
        return 0

    block_ids = [block.id for block in blocks]
    held = (
        await db.execute(
            select(SeatHold.block_id, SeatHold.start_pos, SeatHold.length).where(
                SeatHold.event_id == event_id, SeatHold.block_id.in_(block_ids)
            )
        )
    ).all()

    occupied: dict[int, list[tuple[int, int]]] = {bid: [] for bid in block_ids}
    for block_id, start, length in held:
        occupied[block_id].append((start, start + length))

    runs: list[Run] = []
    for block in blocks:
        cursor = 0
        for lo, hi in sorted(occupied[block.id]):
            if lo > cursor:
                runs.append(Run(block.id, cursor, lo - cursor))
            cursor = max(cursor, hi)
        if cursor < block.capacity:
            runs.append(Run(block.id, cursor, block.capacity - cursor))

    remaining = sum(run.length for run in runs)
    async with redis.pipeline(transaction=True) as pipe:
        pipe.delete(
            _runs_key(event_id, zone_id),
            _ends_key(event_id, zone_id),
            _geom_key(event_id, zone_id),
        )
        pipe.hset(
            _geom_key(event_id, zone_id),
            mapping={
                str(block.id): f"{block.capacity}:{block.quality_base}:{block.quality_edge}"
                for block in blocks
            },
        )
        if runs:
            pipe.hset(
                _runs_key(event_id, zone_id),
                mapping={f"{r.block_id}:{r.start}": r.length for r in runs},
            )
            pipe.hset(
                _ends_key(event_id, zone_id),
                mapping={
                    f"{r.block_id}:{r.start + r.length}": r.start for r in runs
                },
            )
        pipe.set(_zone_available_key(event_id, zone_id), remaining)
        await pipe.execute()
    return remaining


# ─────────────────────────────── 讀（配位與座位圖共用）

async def read_zone_state(redis: Redis, *, event_id: int, zone_id: int) -> ZoneState:
    """一趟 pipeline 讀出空段與幾何。

    唯讀,所以**不進 Lua**:座位圖瀏覽量遠大於下單量,快照過時是可接受的(race
    拒絕使用者能理解,約束拒絕不能),沒必要拿瀏覽流量去佔 Redis 的原子區。
    """
    async with redis.pipeline(transaction=False) as pipe:
        pipe.hgetall(_runs_key(event_id, zone_id))
        pipe.hgetall(_geom_key(event_id, zone_id))
        raw_runs, raw_geom = await pipe.execute()

    geometry: dict[int, BlockGeometry] = {}
    for block_id, packed in raw_geom.items():
        capacity, base, edge = packed.split(":")
        geometry[int(block_id)] = BlockGeometry.calibrated(
            int(block_id), int(capacity), base=float(base), edge=float(edge)
        )

    runs: list[Run] = []
    for field, length in raw_runs.items():
        block_id, start = field.split(":")
        runs.append(Run(int(block_id), int(start), int(length)))
    return ZoneState(runs=runs, geometry=geometry)


async def seat_labels(
    db: AsyncSession, *, block_id: int, start_pos: int, length: int
) -> list[str]:
    """把一個區間翻成人看的門牌。座號不由 hold 直接存,而是 join seats 推導。"""
    return list(
        (
            await db.scalars(
                select(Seat.label)
                .where(
                    Seat.block_id == block_id,
                    Seat.pos >= start_pos,
                    Seat.pos < start_pos + length,
                )
                .order_by(Seat.pos)
            )
        ).all()
    )


# ─────────────────────────────── CAS 佔用

# 對「這個空段還是原樣嗎」做 CAS,然後切下去、扣庫存、入列、寫 claim。
#
# 為什麼比對 (run_start, run_length) 就夠:runs 的不變式是「恰好是極大連續空段」。
# 只要有人從這個空段切走任何一塊,它的 start 會前移、length 會縮短、或分裂成更短
# 的一段 —— (start, length) 這個 pair 必定改變或消失。反之若 pair 沒變,整段就
# 仍然全空(合併只會併入相鄰的空段,不可能把已售出的座位併進來)。所以 ABA 在
# 這裡是良性的:pair 相同 ⟹ 區間可用。
#
# KEYS[1]=runs  KEYS[2]=ends  KEYS[3]=claim  KEYS[4]=stream
# KEYS[5]=zone available  KEYS[6]=event available
# ARGV[1]=block  ARGV[2]=run_start  ARGV[3]=run_length
# ARGV[4]=start  ARGV[5]=length
# ARGV[6]=claim TTL  ARGV[7]=user_id  ARGV[8]=event_id
# ARGV[9]=total_price_cents  ARGV[10]=idempotency_key  ARGV[11]=zone_id
# 回傳:{'DUP'} | {'RETRY'} | {'OK', stream_id, zone_remaining}
_CLAIM_SEATS_LUA = """
local function f(block, pos) return block .. ':' .. string.format('%d', pos) end

local block      = ARGV[1]
local run_start  = tonumber(ARGV[2])
local run_len    = tonumber(ARGV[3])
local start_pos  = tonumber(ARGV[4])
local length     = tonumber(ARGV[5])

-- 1) Dedup:同一個 idempotency_key 已處理過,絕不重複佔位。
if redis.call('EXISTS', KEYS[3]) == 1 then
    return {'DUP'}
end

-- 2) CAS:那個空段必須還是原樣,且我們要的區間必須完整落在它裡面。
local current = redis.call('HGET', KEYS[1], f(block, run_start))
if not current or tonumber(current) ~= run_len then
    return {'RETRY'}
end
if start_pos < run_start or (start_pos + length) > (run_start + run_len) then
    return {'RETRY'}
end

-- 3) 切下去:整段先移除,再寫回 0~2 段殘餘(中切會產生兩段)。
local run_end = run_start + run_len
local left    = start_pos - run_start
local right   = run_end - (start_pos + length)
redis.call('HDEL', KEYS[1], f(block, run_start))
redis.call('HDEL', KEYS[2], f(block, run_end))
if left > 0 then
    redis.call('HSET', KEYS[1], f(block, run_start), left)
    redis.call('HSET', KEYS[2], f(block, start_pos), run_start)
end
if right > 0 then
    redis.call('HSET', KEYS[1], f(block, start_pos + length), right)
    redis.call('HSET', KEYS[2], f(block, run_end), start_pos + length)
end

-- 4) 扣兩個計數器:zone 的(配位用)與 event 的(等候室的 sold_out 訊號用)。
local zone_remaining = redis.call('DECRBY', KEYS[5], length)
redis.call('DECRBY', KEYS[6], length)

-- 5) 入列 + 寫 claim。座位資訊一起帶走,worker 才能建 seat_holds。
local stream_id = redis.call('XADD', KEYS[4], '*',
    'user_id', ARGV[7],
    'event_id', ARGV[8],
    'quantity', ARGV[5],
    'total_price_cents', ARGV[9],
    'idempotency_key', ARGV[10],
    'zone_id', ARGV[11],
    'block_id', ARGV[1],
    'start_pos', ARGV[4])
redis.call('SET', KEYS[3], 'PENDING', 'EX', tonumber(ARGV[6]))

return {'OK', stream_id, tostring(zone_remaining)}
"""

_claim_script: AsyncScript | None = None


# 歸還一個區間並與左右**緊鄰**的空段合併(boundary tags,全 O(1))。
#
# KEYS[1]=runs  KEYS[2]=ends  KEYS[3]=released marker
# KEYS[4]=zone available  KEYS[5]=event available
# ARGV[1]=block  ARGV[2]=start  ARGV[3]=length  ARGV[4]=marker TTL
# 回傳:{'DUP'} | {'OK', merged_start, merged_length, zone_remaining}
_RELEASE_SEATS_LUA = """
local function f(block, pos) return block .. ':' .. string.format('%d', pos) end

-- SETNX 讓重放變成 no-op。這裡它防的不只是多算幾個座位:重複插入同一段會讓
-- 下一次合併把兩個 run 併成宣稱同一批座位的一個 —— 直接導致重複售出。
if not redis.call('SET', KEYS[3], '1', 'NX', 'EX', tonumber(ARGV[4])) then
    return {'DUP'}
end

local block = ARGV[1]
local lo = tonumber(ARGV[2])
local hi = lo + tonumber(ARGV[3])

-- 左鄰:它的 end 等於我的 start(靠 ends 反向索引才查得到)。
local left_start = redis.call('HGET', KEYS[2], f(block, lo))
if left_start then
    redis.call('HDEL', KEYS[1], f(block, left_start))
    redis.call('HDEL', KEYS[2], f(block, lo))
    lo = tonumber(left_start)
end

-- 右鄰:它的 start 等於我的 end。
local right_len = redis.call('HGET', KEYS[1], f(block, hi))
if right_len then
    redis.call('HDEL', KEYS[1], f(block, hi))
    redis.call('HDEL', KEYS[2], f(block, hi + tonumber(right_len)))
    hi = hi + tonumber(right_len)
end

redis.call('HSET', KEYS[1], f(block, lo), hi - lo)
redis.call('HSET', KEYS[2], f(block, hi), lo)

local zone_remaining = redis.call('INCRBY', KEYS[4], tonumber(ARGV[3]))
redis.call('INCRBY', KEYS[5], tonumber(ARGV[3]))
return {'OK', tostring(lo), tostring(hi - lo), tostring(zone_remaining)}
"""

_release_script: AsyncScript | None = None


@dataclass(frozen=True, slots=True)
class SeatReservation:
    """成功佔位的結果。座號刻意不在這裡 —— 確認前不對外揭露。"""

    block_id: int
    start_pos: int
    length: int
    stream_id: str
    zone_remaining: int


async def reserve_seats_and_enqueue(
    redis: Redis,
    *,
    event_id: int,
    zone_id: int,
    user_id: int,
    quantity: int,
    total_price_cents: int,
    idempotency_key: str,
    policy: Policy = NORMAL_POLICY,
    claim_ttl_seconds: int = 86400,
    top_k: int = TOP_K,
    rng: random.Random | None = None,
) -> SeatReservation | None:
    """配一段連續座位、扣庫存、入列 —— 讀-算-CAS。

    回傳 None 代表這個 idempotency_key 已處理過(DUP);其餘失敗以例外表達:
    `NoSeatsAvailable`(配不出來,非售完)或 `SeatContention`(CAS 撞太多次)。
    """
    global _claim_script
    if _claim_script is None:
        _claim_script = redis.register_script(_CLAIM_SEATS_LUA)
    picker = rng or random

    # 收尾期一旦切過就不回頭,所以只在進來時讀一次。
    relaxed = await is_relaxed(redis, event_id=event_id, zone_id=zone_id)
    effective = ENDGAME_POLICY if relaxed else policy

    for attempt in range(1, MAX_CAS_ATTEMPTS + 1):
        state = await read_zone_state(redis, event_id=event_id, zone_id=zone_id)
        anchors = legal_anchors(state.runs, quantity, state.geometry, effective)
        if not anchors:
            if not relaxed and await _record_strict_miss(
                redis, event_id=event_id, zone_id=zone_id
            ):
                # 這個 zone 結構性卡住了(奇偶陷阱:空段都變成 3,兩人票配了會留
                # 孤兒所以被禁,於是 3 席死掉)。切到收尾期並**在同一個請求內**
                # 立刻重試,切換點上就不會有人吃到 409。
                relaxed = True
                effective = ENDGAME_POLICY
                continue
            raise NoSeatsAvailable(
                event_id=event_id,
                zone_id=zone_id,
                quantity=quantity,
                feasible=_feasible(state, effective),
            )

        pick: Placement = anchors[picker.randrange(min(top_k, len(anchors)))]
        host = next(
            run
            for run in state.runs
            if run.block_id == pick.block_id
            and run.start <= pick.start
            and pick.start + pick.length <= run.start + run.length
        )

        result = await _claim_script(
            keys=[
                _runs_key(event_id, zone_id),
                _ends_key(event_id, zone_id),
                _claim_key(idempotency_key),
                ORDER_STREAM_KEY,
                _zone_available_key(event_id, zone_id),
                _event_available_key(event_id),
            ],
            args=[
                pick.block_id, host.start, host.length,
                pick.start, pick.length,
                claim_ttl_seconds, user_id, event_id,
                total_price_cents, idempotency_key, zone_id,
            ],
            client=redis,
        )
        if result[0] == "DUP":
            return None
        if result[0] == "RETRY":
            continue                       # 空段被搶走了 —— 重讀一份快照再算

        # 配得出來 ⇒ 這個 zone 沒卡住,連拒計數歸零。
        await redis.delete(_misses_key(event_id, zone_id))
        zone_remaining = int(result[2])
        if zone_remaining == 0:
            await publish_event_poke(redis, event_id)   # 喚醒等候中的人重讀狀態
        return SeatReservation(
            block_id=pick.block_id,
            start_pos=pick.start,
            length=pick.length,
            stream_id=result[1],
            zone_remaining=zone_remaining,
        )

    raise SeatContention(
        event_id=event_id, zone_id=zone_id, attempts=MAX_CAS_ATTEMPTS
    )


def _feasible(state: ZoneState, policy: Policy) -> list[int]:
    """當下配得出來的張數。注意這不能簡化成「最大連號長度」—— 只剩一段 5 連號時
    4 張是配不出來的(會留下孤兒),回 max_contiguous=5 然後拒絕 4 張正是誤導。"""
    from app.services.seating import feasible_quantities

    return feasible_quantities(
        state.runs, state.geometry, MAX_TICKETS_PER_ORDER, policy
    )


async def release_seats(
    redis: Redis,
    *,
    event_id: int,
    zone_id: int,
    block_id: int,
    start_pos: int,
    length: int,
    marker: str,
    ttl_seconds: int = 86400,
) -> bool:
    """歸還一段座位(含左右合併)。回傳 True 表示這次呼叫真的歸還了。

    `marker` 唯一標識這一次釋放事件("order:{id}" / "dl:{idempotency_key}"),
    讓重放(commit 與 release 之間崩潰、stream 重投、重複取消)成為 no-op。
    """
    global _release_script
    if _release_script is None:
        _release_script = redis.register_script(_RELEASE_SEATS_LUA)
    result = await _release_script(
        keys=[
            _runs_key(event_id, zone_id),
            _ends_key(event_id, zone_id),
            _released_key(marker),
            _zone_available_key(event_id, zone_id),
            _event_available_key(event_id),
        ],
        args=[block_id, start_pos, length, ttl_seconds],
        client=redis,
    )
    if result[0] == "DUP":
        return False
    zone_remaining = int(result[3])
    if zone_remaining - length <= 0 < zone_remaining:   # 從售完回到有票
        await publish_event_poke(redis, event_id)
    return True
