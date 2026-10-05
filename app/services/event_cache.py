"""Event meta cache — cache-aside for the per-order hot read,附 stampede 防護。

下單每次都要讀 event 設定(status / 售票窗 / 價格),但這些在開賣期間是靜態的。
快取在 Redis,大幅減少打 Postgres 的次數。

分區票價也放在這裡而不是每筆訂單查一次 DB:價格表跟其他 meta 一樣在開賣期間
靜態,而下單是最熱的路徑。附帶的好處是「這個 zone 能賣給這個場次嗎」變成一次
dict 查找 —— 見 pricing.load_zone_prices 的白名單語意。

**Stampede 防護是兩層,各擋一半:**

1. process 內:`SingleFlight` —— 同一個 event_id 的併發 miss 只有一個 leader 去重算,
   其餘等同一個結果。單 process 內 N 個 miss → 1 次 DB 讀。
2. 跨 process:Redis lock(SET NX PX + token)—— 各 process 的 leader 再合併一次,
   拿到 lock 的那個讀 DB 並回填,其他 process 的 leader 輪詢快取 key 等它。

兩層疊起來的相乘效果:每個 process 只剩**一個人**去搶 lock、去輪詢,所以第 2 層的
輪詢成本上限是 process 數而不是請求數。第 2 層單獨用的話,輪詢本身就是打在 Redis
上的 stampede。

重算跑在 singleflight 的**獨立 task** 上、開**自己的 session**:發起它的 request 可能
在半路被 RequestTimeoutMiddleware 取消、session 已經關了,重算不能借用它。這也是
`get_event_meta` 不收 `db` 的原因 —— 呼叫端(POST /orders 的請求路徑)因此明確地
不依賴任何 DB session,那本來就是這個端點該有的契約。

快取層的任何失敗都不該變成使用者的 5xx:等不到別人回填就自己讀,寧可多一次 DB 讀。
"""
import asyncio
import json
import logging
import secrets
import time
from dataclasses import dataclass, field
from datetime import datetime

from redis.asyncio import Redis
from redis.commands.core import AsyncScript

from app.core.cache_metrics import EVENT_META_CACHE_FLIGHTS, EVENT_META_CACHE_REQUESTS
from app.core.singleflight import SingleFlight
from app.db.session import CacheSessionLocal
from app.models.event import Event, EventStatus
from app.services.pricing import load_zone_prices

_TTL_SECONDS = 60

#: 跨 process 重算 lock 的 TTL。要遠大於一次重算(PK 點查 + 一個 JOIN,毫秒級),
#: 否則 lock 先過期、第二個 loader 進場,兩個一起讀;也不能太長 —— holder 死掉,
#: 其他 process 最壞要等這麼久才有人接手。
_LOCK_TTL_MS = 3_000
#: follower 輪詢快取 key 的間隔。輪詢者每 process 只有一個(singleflight),
#: 所以總輪詢量的上限是 process 數 / 這個間隔,與請求數無關。
_LOCK_POLL_SECONDS = 0.02
#: 等別人回填的上限,超過就自己讀。等於 lock TTL:過了這麼久 holder 若還沒回填,
#: 它的 lock 也到期了,自己讀不會跟它撞。
_LOCK_WAIT_SECONDS = _LOCK_TTL_MS / 1000

# 只刪自己的 lock:GET 比對 token 再 DEL,原子。裸 DEL 的問題是 —— leader 讀 DB 超過
# lock TTL,lock 過期被別的 process 拿走,這時 leader 的 DEL 刪掉的是**別人的** lock,
# 第三個 loader 又能進場。token 比對就是 fencing。
_RELEASE_LUA = """
if redis.call('GET', KEYS[1]) == ARGV[1] then
    return redis.call('DEL', KEYS[1])
end
return 0
"""
_release_script: AsyncScript | None = None

logger = logging.getLogger(__name__)


@dataclass
class EventMeta:
    event_id: int
    status: EventStatus
    sale_starts_at: datetime
    sale_ends_at: datetime
    price_cents: int | None
    """單一票價。只在 venue_id is None(無座位圖)時使用;座位場次是 None
    (ck_events_price_source)。"""

    venue_id: int | None = None
    zone_prices: dict[int, int] = field(default_factory=dict)
    """zone_id → 單價(分)。key 存在 == 該區屬於本場館且已設價,可以賣。"""


#: process 內的合併點。模組層級單例:一個 process 一個事件迴圈,對應一份登記表。
_flight: SingleFlight[int, EventMeta | None] = SingleFlight()


def _key(event_id: int) -> str:
    return f"event:{event_id}:meta"


def _lock_key(event_id: int) -> str:
    return f"lock:{_key(event_id)}"


def _encode(event: Event, zone_prices: dict[int, int]) -> str:
    return json.dumps({
        "status": event.status.value,
        "sale_starts_at": event.sale_starts_at.isoformat(),
        "sale_ends_at": event.sale_ends_at.isoformat(),
        "price_cents": event.price_cents,
        "venue_id": event.venue_id,
        "zone_prices": zone_prices,
    })


def _decode(event_id: int, raw: str) -> EventMeta:
    d = json.loads(raw)
    return EventMeta(
        event_id=event_id,
        status=EventStatus(d["status"]),
        sale_starts_at=datetime.fromisoformat(d["sale_starts_at"]),
        sale_ends_at=datetime.fromisoformat(d["sale_ends_at"]),
        price_cents=d["price_cents"],
        venue_id=d.get("venue_id"),
        # JSON 的物件 key 一定是字串,轉回 int 否則每次查找都 miss ——
        # 那會讓每一個 zone 都變成「不可賣」,而且沒有任何錯誤訊息。
        zone_prices={int(k): v for k, v in d.get("zone_prices", {}).items()},
    )


async def get_event_meta(redis: Redis, *, event_id: int) -> EventMeta | None:
    """Cache-aside:先讀 Redis,miss 才(合併後)回 Postgres 並回填。"""
    cached = await redis.get(_key(event_id))
    if cached is not None:
        EVENT_META_CACHE_REQUESTS.labels(outcome="hit").inc()
        return _decode(event_id, cached)
    EVENT_META_CACHE_REQUESTS.labels(outcome="miss").inc()
    return await _flight.run(event_id, lambda: _load_logged(redis, event_id))


async def _load_logged(redis: Redis, event_id: int) -> EventMeta | None:
    """flight 的失敗要留痕。等它的請求可能早已超時離場,而 shield 會把沒人取回的例外
    標成「已取回」—— 不在這裡記一筆,重算掛掉就是完全無聲的(2026-09-15 的 pool 死鎖
    就是這樣:149 個 504,log 裡零例外)。"""
    try:
        return await _load_coordinated(redis, event_id)
    except Exception:
        logger.exception(
            "event meta recompute failed; waiters (if any) get this exception",
            extra={"event": "event_meta_cache_load_failed", "event_id": event_id},
        )
        raise


async def _load_coordinated(redis: Redis, event_id: int) -> EventMeta | None:
    """process 內的 leader 走這裡:用 Redis lock 跟其他 process 的 leader 再合併一次。

    整段跑在 singleflight 的獨立 task 上,所以不會有「取消發生在 SET NX 之後、釋放
    之前」留下孤兒 lock 的情況 —— 等待者的取消到不了這裡。
    """
    key, lock_key = _key(event_id), _lock_key(event_id)
    token = secrets.token_hex(8)
    deadline = time.monotonic() + _LOCK_WAIT_SECONDS

    while True:
        if await redis.set(lock_key, token, nx=True, px=_LOCK_TTL_MS):
            try:
                # double-check:從我們 miss 到拿到 lock 之間,別的 process 可能已經
                # 回填並釋放了。少了這一步,「前一個 leader 剛做完」會變成多讀一次。
                cached = await redis.get(key)
                if cached is not None:
                    EVENT_META_CACHE_FLIGHTS.labels(resolution="already_filled").inc()
                    return _decode(event_id, cached)
                # 記在讀之前:失敗的讀也花了 round-trip,「DB 被打了幾次」要算它。
                EVENT_META_CACHE_FLIGHTS.labels(resolution="loaded").inc()
                return await _load_and_fill(redis, event_id)
            finally:
                await _release_lock(redis, lock_key, token)

        # 別的 process 正在讀。輪詢的是快取 key 不是 lock:值一寫進去就走,不必等
        # 它釋放 lock。先看再睡 —— 它很可能剛好做完了。
        cached = await redis.get(key)
        if cached is not None:
            EVENT_META_CACHE_FLIGHTS.labels(resolution="waited").inc()
            return _decode(event_id, cached)
        if time.monotonic() >= deadline:
            # holder 死了,或慢到超過 TTL。降級成自己讀:多一次 DB 讀,絕不讓請求失敗。
            # 但要留下痕跡:一次是雜訊,持續出現就是有 process 死在重算裡、或 DB 慢到
            # 一次點查超過 lock TTL —— 兩者都不是快取層能修的,要有人看。
            EVENT_META_CACHE_FLIGHTS.labels(resolution="fallback").inc()
            logger.warning(
                "event meta lock holder never filled the cache; loading without the lock",
                extra={
                    "event": "event_meta_cache_wait_exhausted",
                    "event_id": event_id,
                    "waited_seconds": _LOCK_WAIT_SECONDS,
                },
            )
            return await _load_and_fill(redis, event_id)
        await asyncio.sleep(_LOCK_POLL_SECONDS)


async def _load_and_fill(redis: Redis, event_id: int) -> EventMeta | None:
    """真的讀 DB 並回填。

    走 **CacheSessionLocal(獨立的小 pool)**,不走請求用的主 pool:等這個 flight 的每個
    請求都握著一條主 pool 連線(auth 讀過 user 之後就一直握到 request 結束),loader 若
    也向主 pool 要,pool 一滿就是互等 —— 見 db/session.py 的 bulkhead 說明。

    session 在 Redis round-trip 之前就關掉:不要抱著一條 pool 連線等網路。
    """
    async with CacheSessionLocal() as db:
        event = await db.get(Event, event_id)
        if event is None:
            return None
        zone_prices = await load_zone_prices(
            db, event_id=event_id, venue_id=event.venue_id
        )
    await redis.set(_key(event_id), _encode(event, zone_prices), ex=_TTL_SECONDS)
    return EventMeta(
        event_id=event_id,
        status=event.status,
        sale_starts_at=event.sale_starts_at,
        sale_ends_at=event.sale_ends_at,
        price_cents=event.price_cents,
        venue_id=event.venue_id,
        zone_prices=zone_prices,
    )


async def _release_lock(redis: Redis, lock_key: str, token: str) -> bool:
    """釋放 lock,**只在它還是我們的時候**。回傳是否真的刪了。"""
    global _release_script
    if _release_script is None:
        _release_script = redis.register_script(_RELEASE_LUA)
    return bool(await _release_script(keys=[lock_key], args=[token], client=redis))


async def invalidate_event_meta(redis: Redis, *, event_id: int) -> None:
    """活動狀態改變時清掉快取(例如發佈)。

    改分區票價也必須呼叫這個 —— 否則最多 _TTL_SECONDS 內還會按舊價賣。

    只刪快取 key,不動 lock:lock 保護的是一次進行中的重算,刪了它只會放第二個
    loader 進場。
    """
    await redis.delete(_key(event_id))
