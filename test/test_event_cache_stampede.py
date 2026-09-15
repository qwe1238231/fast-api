"""Cache stampede on the event-meta cache — 兩層防護各自的 regression guard。

`get_event_meta` 是 cache-aside:miss 就回 Postgres 重算再回填。開賣瞬間 key 冷
(剛過期、或剛被 invalidate)時,N 個併發請求全部同時 miss —— 沒有防護的話 DB 就被
讀 N 次(2026-09-15 量到的基準是 19~20 / 20,差的那一次是最後一個 GET 剛好落在
第一次回填之後)。

數的是送到 Postgres 的 SQL 條數,不是 Python function 被叫幾次:成本發生在
round-trip 與 pool checkout,而且 SQL 計數不會因為重構 event_cache 內部而失效。

第 1 層(process 內 singleflight)用真的併發 burst 測;第 2 層(跨 process 的 Redis
lock)沒有第二個 process 可用,改用「手動放一把別人的 lock」模擬另一個 process 正在
重算,驗的是我們這一側的行為:不讀 DB、等回填、不碰別人的 lock、等不到就降級。
"""
import asyncio
from collections.abc import Iterator
from contextlib import contextmanager
from typing import Any

import pytest
from prometheus_client import REGISTRY
from redis.asyncio import Redis
from sqlalchemy import event
from sqlalchemy.engine import Connection

from app.core.config import get_settings
from app.db.session import cache_engine, engine
from app.services import event_cache
from app.services.event_cache import EventMeta, get_event_meta

N = 20

#: 兩個 counter 的六條 series。指標是 process 全域且跨測試累加,所以只能量**差值**。
_SERIES: dict[str, tuple[str, dict[str, str]]] = {
    "requests.hit": ("event_meta_cache_requests_total", {"outcome": "hit"}),
    "requests.miss": ("event_meta_cache_requests_total", {"outcome": "miss"}),
    "flights.loaded": ("event_meta_cache_flights_total", {"resolution": "loaded"}),
    "flights.already_filled": ("event_meta_cache_flights_total", {"resolution": "already_filled"}),
    "flights.waited": ("event_meta_cache_flights_total", {"resolution": "waited"}),
    "flights.fallback": ("event_meta_cache_flights_total", {"resolution": "fallback"}),
}


def _snapshot() -> dict[str, float]:
    return {
        key: REGISTRY.get_sample_value(name, labels) or 0.0
        for key, (name, labels) in _SERIES.items()
    }


def _moved(before: dict[str, float]) -> dict[str, float]:
    """這段期間有增量的 series。沒動的不出現 —— 一個 == 斷言就同時涵蓋「該動的
    動了」與「不該動的沒動」。"""
    after = _snapshot()
    return {key: after[key] - before[key] for key in _SERIES if after[key] != before[key]}


@contextmanager
def count_statements(needle: str) -> Iterator[list[str]]:
    """收集這段期間送到 DB、且含 `needle` 的 SQL。

    回 list 不回 int:斷言失敗時要看得到是哪幾條。`before_cursor_execute` 對 asyncpg
    一樣會觸發 —— 它掛在 dialect 之上的 Core 層,掛到 `engine.sync_engine` 即可。
    """
    seen: list[str] = []

    def _on_execute(
        conn: Connection,
        cursor: Any,
        statement: str,
        parameters: Any,
        context: Any,
        executemany: bool,
    ) -> None:
        if needle in statement:
            seen.append(statement)

    # 兩個 engine 都掛:重算走 cache_engine(bulkhead pool),請求走主 engine。只掛一邊
    # 會把另一邊的 DB 讀漏數成 0 —— 那正是「量出零次讀、其實是量錯」的形狀。
    engines = (engine.sync_engine, cache_engine.sync_engine)
    for e in engines:
        event.listen(e, "before_cursor_execute", _on_execute)
    try:
        yield seen
    finally:
        for e in engines:
            event.remove(e, "before_cursor_execute", _on_execute)


async def _burst(redis: Redis, event_id: int) -> list[EventMeta | None]:
    """N 個併發呼叫。呼叫端不再需要 session:重算的 session 由 event_cache 自己開。"""
    return await asyncio.gather(
        *(get_event_meta(redis, event_id=event_id) for _ in range(N))
    )


# ── 第 1 層:process 內 singleflight ──────────────────────────────────────────

@pytest.mark.asyncio
async def test_cold_key_burst_loads_db_once(redis, published_event):
    """冷 key 的 N 個併發 miss 只讓 DB 重算一次,大家拿到同一份,lock 用完就放。"""
    before = _snapshot()
    with count_statements("FROM events") as loads:
        metas = await _burst(redis, published_event.id)
    assert len(loads) == 1, f"{len(loads)} DB loads for one cold key"
    assert all(m is not None and m.event_id == published_event.id for m in metas)
    assert await redis.get(event_cache._lock_key(published_event.id)) is None

    moved = _moved(before)
    assert moved["flights.loaded"] == 1 and "flights.fallback" not in moved
    # 極少數 GET 會落在回填之後變成 hit(step 1 量到的 19/20),所以只釘總數
    assert moved["requests.miss"] + moved.get("requests.hit", 0) == N


@pytest.mark.asyncio
async def test_warm_key_burst_never_touches_db(redis, published_event):
    """暖 key 的 burst 完全不碰 DB —— 問題只在冷 key,hit path 沒事。"""
    await get_event_meta(redis, event_id=published_event.id)               # 暖 key

    before = _snapshot()
    with count_statements("FROM events") as loads:
        await _burst(redis, published_event.id)
    assert loads == []
    assert _moved(before) == {"requests.hit": N}


@pytest.mark.asyncio
async def test_cancelled_waiters_do_not_abort_the_fill(redis, published_event):
    """一半的等待者被取消(RequestTimeoutMiddleware 砍 request 的情境):剩下的
    照樣拿到值,DB 仍只讀一次,lock 沒有變成孤兒。"""
    before = _snapshot()
    with count_statements("FROM events") as loads:
        waiters = [
            asyncio.create_task(get_event_meta(redis, event_id=published_event.id))
            for _ in range(N)
        ]
        while len(event_cache._flight) == 0:      # 等到第一個 miss 開了 flight
            await asyncio.sleep(0)
        for w in waiters[: N // 2]:               # 發起者幾乎一定在這一半裡
            w.cancel()
        survivors = await asyncio.gather(*waiters[N // 2:])
        await asyncio.gather(*waiters[: N // 2], return_exceptions=True)

    assert all(m is not None for m in survivors)
    assert len(loads) == 1
    assert await redis.get(event_cache._lock_key(published_event.id)) is None
    assert _moved(before)["flights.loaded"] == 1      # 被取消的 GET 有沒有算進 miss 不確定,只釘讀數


# ── 第 2 層:跨 process 的 Redis lock ─────────────────────────────────────────

@pytest.mark.asyncio
async def test_follower_process_waits_for_the_fill_instead_of_loading(redis, published_event):
    """別的 process 正持有 lock:我們不讀 DB、輪詢等它回填,而且一根毛都不動它的 lock。"""
    key = event_cache._key(published_event.id)
    lock = event_cache._lock_key(published_event.id)
    await redis.set(lock, "other-process", px=event_cache._LOCK_TTL_MS)

    before = _snapshot()
    with count_statements("FROM events") as loads:
        waiter = asyncio.create_task(get_event_meta(redis, event_id=published_event.id))
        await asyncio.sleep(event_cache._LOCK_POLL_SECONDS * 3)
        assert not waiter.done()                                          # 還在等,沒自己去讀

        await redis.set(key, event_cache._encode(published_event, {}), ex=60)   # 「別人」回填了
        meta = await asyncio.wait_for(waiter, timeout=1)

    assert loads == []
    assert meta is not None and meta.status.value == "published"
    assert await redis.get(lock) == "other-process"
    assert _moved(before) == {"requests.miss": 1, "flights.waited": 1}


@pytest.mark.asyncio
async def test_double_check_after_acquiring_the_lock_skips_the_load(redis, published_event, monkeypatch):
    """前一個 leader 剛做完(回填 + 釋放)的瞬間我們拿到 lock:要再看一次快取,
    不能直接讀 DB —— 少了 double-check,「剛做完」會變成多讀一次。"""
    monkeypatch.setattr(event_cache, "_LOCK_POLL_SECONDS", 0.2)   # 把它睡的窗拉大,好在裡面動手
    key = event_cache._key(published_event.id)
    lock = event_cache._lock_key(published_event.id)
    await redis.set(lock, "previous-leader", px=event_cache._LOCK_TTL_MS)

    before = _snapshot()
    with count_statements("FROM events") as loads:
        waiter = asyncio.create_task(get_event_meta(redis, event_id=published_event.id))
        await asyncio.sleep(0.05)                                         # 它現在在 200ms 的 sleep 裡
        async with redis.pipeline(transaction=True) as pipe:              # 「前一個 leader」回填並釋放
            pipe.set(key, event_cache._encode(published_event, {}), ex=60)
            pipe.delete(lock)
            await pipe.execute()
        meta = await asyncio.wait_for(waiter, timeout=1)                  # 醒來 → 拿到 lock → double-check 命中

    assert loads == []
    assert meta is not None and meta.status.value == "published"
    assert await redis.get(lock) is None                                  # 自己拿的 lock 自己放
    assert _moved(before) == {"requests.miss": 1, "flights.already_filled": 1}


@pytest.mark.asyncio
async def test_dead_lock_holder_degrades_to_own_load_after_the_wait(
    redis, published_event, monkeypatch, caplog
):
    """holder 死了、lock 還在、永遠不會有人回填:等到上限就自己讀,絕不讓請求失敗。"""
    monkeypatch.setattr(event_cache, "_LOCK_WAIT_SECONDS", 0.1)
    lock = event_cache._lock_key(published_event.id)
    await redis.set(lock, "dead-process", px=10_000)                      # 比等待上限長很多

    before = _snapshot()
    with count_statements("FROM events") as loads:
        meta = await asyncio.wait_for(
            get_event_meta(redis, event_id=published_event.id), timeout=2
        )
    assert meta is not None
    assert len(loads) == 1
    assert await redis.get(event_cache._key(published_event.id)) is not None   # 還是回填了
    assert _moved(before) == {"requests.miss": 1, "flights.fallback": 1}
    # 降級要留痕:持續出現代表有 process 死在重算裡、或 DB 慢過 lock TTL,快取層修不了
    assert "event_meta_cache_wait_exhausted" in {getattr(r, "event", None) for r in caplog.records}


@pytest.mark.asyncio
async def test_lock_is_released_only_by_its_owner(redis):
    """fencing:token 不對就不刪。leader 超時後刪到別人的 lock,會放第三個 loader 進場。"""
    lock = event_cache._lock_key(999)
    await redis.set(lock, "theirs", px=10_000)

    assert await event_cache._release_lock(redis, lock, "mine") is False
    assert await redis.get(lock) == "theirs"

    assert await event_cache._release_lock(redis, lock, "theirs") is True
    assert await redis.get(lock) is None


@pytest.mark.asyncio
async def test_recompute_survives_an_exhausted_request_pool(redis, published_event):
    """死鎖回歸。主 pool 被握滿 —— 每個請求 auth 之後都握著一條連線等 flight —— loader 若
    也向主 pool 要連線就是互等:2026-09-15 對 4 workers 打 300 併發冷 key,149 個 504、
    DB 讀 0 次、log 零例外。重算走獨立的 cache pool,所以主 pool 滿了也照樣完成。

    修掉之前這條會等到 asyncio.wait_for 的 5 秒(主 pool 的 pool_timeout 是 30 秒)。"""
    settings = get_settings()
    capacity = settings.DB_POOL_SIZE + settings.DB_MAX_OVERFLOW
    held = [await engine.connect() for _ in range(capacity)]      # 把主 pool 握滿
    try:
        meta = await asyncio.wait_for(
            get_event_meta(redis, event_id=published_event.id), timeout=5
        )
    finally:
        for conn in held:
            await conn.close()
    assert meta is not None and meta.event_id == published_event.id


# ── 邊界 ──────────────────────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_unknown_event_is_none_and_not_cached(redis):
    """不存在的 event 回 None,而且**今天不做 negative cache** —— 每次都會穿透到 DB。
    這條釘住現況,讓之後加 negative cache 是一個明確的決定而不是順手的副作用。"""
    before = _snapshot()
    with count_statements("FROM events") as loads:
        assert await get_event_meta(redis, event_id=987_654) is None
    assert len(loads) == 1
    assert _moved(before) == {"requests.miss": 1, "flights.loaded": 1}   # 讀了 DB 就算 loaded,回不回填是另一回事
    assert await redis.get(event_cache._key(987_654)) is None
    assert await redis.get(event_cache._lock_key(987_654)) is None
