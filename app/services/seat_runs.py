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
import time
from collections.abc import Sequence
from dataclasses import dataclass

from redis.asyncio import Redis
from redis.commands.core import AsyncScript
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.seat_metrics import (
    SEAT_CAS_ATTEMPTS,
    SEAT_CAS_ATTEMPTS_PER_RESERVATION,
    SEAT_CAS_WINDOW,
    SEAT_CONTENTION,
)
from app.core.config import get_settings, max_purchasable
from app.core.exceptions import (
    InsufficientInventory,
    InventoryNotReconcilable,
    NoSeatsAvailable,
    PurchaseLimitExceeded,
    SeatContention,
    SeatPlacementOutOfRun,
    SeatReleaseOverlap,
)
from app.models.seating import Seat, SeatBlock, SeatHold
from app.services.idempotency import _key as _claim_key
from app.services.inventory import (
    ORDER_STREAM_KEY,
    _key as _event_available_key,
    _purchased_key,
    _released_key,
    queue_depth,
)
from app.services.queue_events import publish_event_poke
from app.services.seating import (
    ENDGAME_POLICY,
    NORMAL_POLICY,
    BlockGeometry,
    Placement,
    Policy,
    Run,
    feasible_quantities,
    legal_anchors,
)

#: 從前 K 個候選隨機挑,壓低 CAS 衝突。K=1 等於純決定性(高併發下會 thundering
#: herd);K=16 實測把 λ=2000 的重試放大從 3.08× 壓到 1.03×,品質只差 0.117 座。
TOP_K = 16

#: CAS 重試上限。超過就當成暫時性失敗往上拋 —— 有界重試,不要在熱路徑上無限迴圈。
MAX_CAS_ATTEMPTS = 5

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

#: 連拒計數的存活時間。有它之後語意才精確是「**最近**連續被拒幾筆」——
#: 沒有的話,一個沒流量的 zone 每小時被拒一筆、三小時後也會觸發。順便讓這個
#: 計數器不會在活動結束後永久留在 Redis。
STRICT_MISS_TTL_SECONDS = 300


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


async def is_relaxed(redis: Redis, *, event_id: int, zone_id: int) -> bool:
    """這個 zone 是否已經切進收尾期策略。單向 —— 一旦切了就不回頭。"""
    return bool(await redis.exists(_relaxed_key(event_id, zone_id)))


async def _record_strict_miss(redis: Redis, *, event_id: int, zone_id: int) -> bool:
    """記一次「嚴格策略配不出來」。回傳 True 表示該切到收尾期了。

    不做原子化:INCR 與 SET 之間的競態最多讓兩個請求同時把 ratchet 打開,而 SET
    本來就是冪等的。為了一個單向的布林旗標開 Lua 不划算。
    """
    key = _misses_key(event_id, zone_id)
    misses = await redis.incr(key)
    await redis.expire(key, STRICT_MISS_TTL_SECONDS)
    if misses < STRICT_MISS_THRESHOLD:
        return False
    await redis.set(_relaxed_key(event_id, zone_id), "1")
    await redis.delete(key)          # 已經切了,這個計數器不再有意義
    return True


@dataclass(frozen=True, slots=True)
class ZoneSnapshot:
    """一個 zone 的空段、幾何、以及它是否已切進收尾期策略。

    三者一起讀是因為呼叫端(選區畫面)三個都要,而分開讀就是每個 zone 三次序列
    往返 —— 100 個 zone 的場館 300 次。見 `read_zone_snapshots`。
    """

    state: "ZoneState"
    relaxed: bool


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
    db: AsyncSession,
    redis: Redis,
    *,
    event_id: int,
    zone_id: int,
    force: bool = False,
) -> int:
    """從 Postgres 重算整個 zone 的空段結構並覆寫 Redis。回傳剩餘席數。

    這是 Redis 遺失、或偵測到結構損壞(seat_holds 的 EXCLUDE 拒絕了一筆 worker
    落帳)之後唯一安全的復原方式。**不要試圖「退還」那筆被拒的區間** —— EXCLUDE
    拒絕代表區間與別人的 hold 重疊,而你無法從被拒的那筆判斷哪一段是誰的;盲目
    退還會把別人持有的座位標回可賣,把「DB 拒絕一筆」升級成真的兩人同座。

    重建則自動涵蓋了「該退的座位」:DB 沒有紀錄的座位就是空的。

    **stream 未排空時拒絕執行**,跟 reconcile_inventory 一樣的守衛:in-flight 的
    intent 持有 Redis 認定的區間但 DB 還沒有,此時重建會把它們算成空的 —— 那些座位
    會被賣第二次,而原本的 intent 落帳時撞上 EXCLUDE。這是整個功能最容易誤觸的一顆
    地雷(ops 想「修一下漂移」就會踩到),所以守衛必須在程式裡而不是在註解裡。

    `force=True` 只在確定沒有 in-flight intent 指向這個 event 時才可以用 ——
    例如 publish 當下:場次還沒開賣,不可能有訂單。
    """
    if not force:
        backlog, dead = await queue_depth(redis)
        if backlog:
            raise InventoryNotReconcilable(event_id, backlog, dead)

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

    但兩個 HGETALL 之間**必須**原子(MULTI/EXEC,成本一樣是一趟往返):否則它們
    可能夾著 rebuild_zone_runs 的 EXEC,讀到「新的 runs + 舊的 geom」。如果那次
    rebuild 改變了 block 集合,`geometry[run.block_id]` 就會 KeyError —— 熱路徑 500。
    """
    async with redis.pipeline(transaction=True) as pipe:
        pipe.hgetall(_runs_key(event_id, zone_id))
        pipe.hgetall(_geom_key(event_id, zone_id))
        raw_runs, raw_geom = await pipe.execute()
    return _parse_state(raw_runs, raw_geom)


def _parse_state(raw_runs: dict, raw_geom: dict) -> ZoneState:
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


async def read_zone_snapshots(
    redis: Redis, *, event_id: int, zone_ids: Sequence[int]
) -> dict[int, ZoneSnapshot]:
    """一趟 pipeline 讀出多個 zone 的完整狀態 —— 選區畫面用。

    對每個 zone 發三個指令(runs / geom / relaxed),但**全部在同一趟往返**裡。
    逐個 zone 呼叫 `read_zone_state` + `is_relaxed` 的話是 3N 次序列往返:100 個
    zone 的場館就是 300 次,而這是整個系統最常被瀏覽的端點。
    """
    if not zone_ids:
        return {}
    async with redis.pipeline(transaction=True) as pipe:
        for zone_id in zone_ids:
            pipe.hgetall(_runs_key(event_id, zone_id))
            pipe.hgetall(_geom_key(event_id, zone_id))
            pipe.exists(_relaxed_key(event_id, zone_id))
        results = await pipe.execute()

    out: dict[int, ZoneSnapshot] = {}
    for index, zone_id in enumerate(zone_ids):
        raw_runs, raw_geom, relaxed = results[index * 3 : index * 3 + 3]
        out[zone_id] = ZoneSnapshot(
            state=_parse_state(raw_runs, raw_geom), relaxed=bool(relaxed)
        )
    return out


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
# 回傳:{'DUP'} | {'RETRY'} | {'BOUNDS'} | {'OK', stream_id, zone_remaining}
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

-- 1b) 每人限購。放在 CAS **之前**是刻意的:放後面的話,一個已達上限的人在熱門
--     場次會先撞幾次 RETRY 才拿到 OVER_LIMIT —— 白跑幾輪讀-算-CAS,而且那些
--     RETRY 會被記成競爭,污染 T 的樣本。放前面則第一次就給終局答案。
local limit = tonumber(ARGV[12])
local held = tonumber(redis.call('HGET', KEYS[7], ARGV[7])) or 0
if held + length > limit then
    return {'OVER_LIMIT', tostring(held), tostring(limit)}
end

-- 2) CAS:那個空段必須還是原樣,且我們要的區間必須完整落在它裡面。
local current = redis.call('HGET', KEYS[1], f(block, run_start))
if not current or tonumber(current) ~= run_len then
    return {'RETRY'}
end
-- 界外是**呼叫端的 bug**(配位算出的區間不在它取材的空段裡),不是競爭。回 RETRY
-- 會讓它重試到變成 SeatContention(503),於是客戶端一直重送同一個壞請求。
if start_pos < run_start or (start_pos + length) > (run_start + run_len) then
    return {'BOUNDS'}
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

-- 4) 扣三個計數器:zone 的(配位用)、event 的(等候室的 sold_out 訊號用),
--    以及這個人的限購額度。額度必須跟庫存同進同出。
local zone_remaining = redis.call('DECRBY', KEYS[5], length)
redis.call('DECRBY', KEYS[6], length)
redis.call('HINCRBY', KEYS[7], ARGV[7], length)

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
# KEYS[4]=zone available  KEYS[5]=event available  KEYS[6]=geom
# ARGV[1]=block  ARGV[2]=start  ARGV[3]=length  ARGV[4]=marker TTL
# 回傳:{'DUP'} | {'OVERLAP', run_start, run_length} | {'OOB', capacity}
#      | {'OK', merged_start, merged_length, zone_remaining}
_RELEASE_SEATS_LUA = """
local function f(block, pos) return block .. ':' .. string.format('%d', pos) end

-- 1) 重放檢查必須在驗證之前。第一次釋放成功之後那個區間就是空的,所以一個合法的
--    重放若先跑驗證會被誤判成 OVERLAP。marker 讓重放成為 no-op,而它防的不只是
--    多算幾個座位:重複插入同一段會讓下一次合併把兩個 run 併成宣稱同一批座位的
--    一個 —— 直接導致重複售出。
if redis.call('EXISTS', KEYS[3]) == 1 then
    return {'DUP'}
end

local block = ARGV[1]
local lo = tonumber(ARGV[2])
local hi = lo + tonumber(ARGV[3])

-- 2a) 上界:區間必須落在 block 裡面。落在外面的話它不與任何空段相交,所以下面
--     那道重疊檢查放它過去 —— 然後寫出一段超出容量的假空段,那些「座位」會被
--     配給真實的訂單。geom 存了容量,所以這是一次 HGET 的事。
local geom = redis.call('HGET', KEYS[6], block)
if geom then
    local capacity = tonumber(string.match(geom, '^(%d+):'))
    if lo < 0 or hi > capacity then
        return {'OOB', tostring(capacity)}
    end
end

-- 2b) 驗證:歸還的區間必須完全是「已佔用」的,即不與任何既有空段相交。
--    刻意用 O(R) 掃描而不是 O(1) 邊界查詢 —— 一個被既有空段**完整包含**的區間
--    (例如空段 [0,10) 裡的 [2,4))用邊界比對永遠找不到,而那正是最危險的情況:
--    它會寫出兩段互相重疊的空段,而 runs/ends 一致性檢查抓不到(重疊 runs 推導出
--    的 ends 剛好就是實際的 ends)。release 不在搶票熱路徑上(過期掃描、取消、
--    dead-letter),付得起這個掃描;配位那支 Lua 仍然是 O(1)。
local all = redis.call('HGETALL', KEYS[1])
for i = 1, #all, 2 do
    local sep = string.find(all[i], ':')
    if string.sub(all[i], 1, sep - 1) == block then
        local run_lo = tonumber(string.sub(all[i], sep + 1))
        local run_hi = run_lo + tonumber(all[i + 1])
        if run_lo < hi and lo < run_hi then
            return {'OVERLAP', tostring(run_lo), tostring(run_hi - run_lo)}
        end
    end
end

-- 3) 通過驗證才寫 marker。反過來的話,一次拿錯區間的呼叫會燒掉 marker,使後續
--    正確的釋放變成 DUP no-op —— 座位就永遠回不來了。
redis.call('SET', KEYS[3], '1', 'EX', tonumber(ARGV[4]))

-- 3b) 退限購額度。在 marker 底下所以最多退一次;歸零就刪欄位,那同時是垃圾回收
--     與負值的地板(功能上線前建立的舊訂單從來沒加過額度)。
local left = redis.call('HINCRBY', KEYS[7], ARGV[5], -tonumber(ARGV[3]))
if left <= 0 then
    redis.call('HDEL', KEYS[7], ARGV[5])
end

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
    max_per_user: int | None = None,
) -> SeatReservation | None:
    """配一段連續座位、扣庫存、入列 —— 讀-算-CAS。

    回傳 None 代表這個 idempotency_key 已處理過(DUP);其餘失敗以例外表達:
    `NoSeatsAvailable`(配不出來,非售完)或 `SeatContention`(CAS 撞太多次)。
    """
    global _claim_script
    if _claim_script is None:
        _claim_script = redis.register_script(_CLAIM_SEATS_LUA)
    picker = rng or random
    if max_per_user is None:
        max_per_user = get_settings().MAX_TICKETS_PER_USER_PER_EVENT

    # 收尾期一旦切過就不回頭,所以只在進來時讀一次。
    relaxed = await is_relaxed(redis, event_id=event_id, zone_id=zone_id)
    effective = ENDGAME_POLICY if relaxed else policy

    for attempt in range(1, MAX_CAS_ATTEMPTS + 1):
        # T 的定義:從讀快照開始,到 CAS 回來為止 —— 那正是「別人能讓我們這份
        # 快照過時」的區間,也就是 N = λ × T 裡的那個 T。
        window_started = time.perf_counter()
        state = await read_zone_state(redis, event_id=event_id, zone_id=zone_id)
        anchors = legal_anchors(state.runs, quantity, state.geometry, effective)
        if not anchors:
            # 先分清「沒票了」與「湊不出連號」。以前兩者都回 NoSeatsAvailable,於是
            # 真正賣完的 zone 被告知「湊不出連號」,而且跟無座位圖路徑的
            # InsufficientInventory 不一致。順序也重要:賣完的 zone 記連拒次數是
            # 沒意義的(放寬也配不出來),還會污染 ratchet 的訊號。
            # 這條路徑沒有做 CAS,所以**不是** T 的樣本:沒有人能讓一份從未被用來
            # 佔位的快照過時。把它算進去只會用系統性較短的樣本把 p99 拉低 ——
            # 那是安全指標最不該有的偏誤方向。
            if state.remaining < quantity:
                raise InsufficientInventory(
                    event_id=event_id, requested=quantity, available=state.remaining,
                )
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
                _purchased_key(event_id),
            ],
            args=[
                pick.block_id, host.start, host.length,
                pick.start, pick.length,
                claim_ttl_seconds, user_id, event_id,
                total_price_cents, idempotency_key, zone_id,
                max_per_user,
            ],
            client=redis,
        )
        SEAT_CAS_WINDOW.observe(time.perf_counter() - window_started)
        SEAT_CAS_ATTEMPTS.labels(outcome=result[0].lower()).inc()

        if result[0] == "DUP":
            return None
        if result[0] == "OVER_LIMIT":
            # 終局:重送幾次都一樣。**不要** continue —— 那會讓它撞滿重試次數後回
            # SeatContention(503),客戶端讀到「暫時性、請重試」而無限重送。
            raise PurchaseLimitExceeded(
                event_id=event_id, user_id=user_id, requested=quantity,
                already=int(result[1]), limit=int(result[2]),
            )
        if result[0] == "BOUNDS":
            raise SeatPlacementOutOfRun(
                event_id=event_id, zone_id=zone_id, block_id=pick.block_id,
                start=pick.start, length=pick.length,
            )
        if result[0] == "RETRY":
            continue                       # 空段被搶走了 —— 重讀一份快照再算

        # 配得出來 ⇒ 這個 zone 沒卡住,連拒計數歸零。
        await redis.delete(_misses_key(event_id, zone_id))
        SEAT_CAS_ATTEMPTS_PER_RESERVATION.observe(attempt)
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

    SEAT_CONTENTION.inc()
    raise SeatContention(
        event_id=event_id, zone_id=zone_id, attempts=MAX_CAS_ATTEMPTS
    )


def _feasible(state: ZoneState, policy: Policy) -> list[int]:
    """當下配得出來的張數。注意這不能簡化成「最大連號長度」—— 只剩一段 5 連號時
    4 張是配不出來的(會留下孤兒),回 max_contiguous=5 然後拒絕 4 張正是誤導。"""
    return feasible_quantities(
        state.runs, state.geometry, max_purchasable(), policy
    )


async def release_seats(
    redis: Redis,
    *,
    event_id: int,
    zone_id: int,
    user_id: int,
    block_id: int,
    start_pos: int,
    length: int,
    marker: str,
    ttl_seconds: int = 86400,
) -> bool:
    """歸還一段座位(含左右合併)與該買家的限購額度。回傳 True 表示真的歸還了。

    `marker` 唯一標識這一次釋放事件("order:{id}" / "dl:{idempotency_key}"),
    讓重放(commit 與 release 之間崩潰、stream 重投、重複取消)成為 no-op ——
    額度退回也在同一個 marker 底下,所以同樣最多一次。

    `user_id` 必填的理由見 `inventory.release`:漏退額度的症狀是那個人再也買不了,
    而且不會有任何錯誤訊息。
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
            _geom_key(event_id, zone_id),
            _purchased_key(event_id),
        ],
        args=[block_id, start_pos, length, ttl_seconds, user_id],
        client=redis,
    )
    if result[0] == "DUP":
        return False
    if result[0] in ("OVERLAP", "OOB"):
        raise SeatReleaseOverlap(
            event_id=event_id, zone_id=zone_id,
            block_id=block_id, start=start_pos, length=length,
            reason="out of block bounds" if result[0] == "OOB" else "intersects a free run",
        )
    zone_remaining = int(result[3])
    if zone_remaining - length <= 0 < zone_remaining:   # 從售完回到有票
        await publish_event_poke(redis, event_id)
    return True
