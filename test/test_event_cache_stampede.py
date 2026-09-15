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
from redis.asyncio import Redis
from sqlalchemy import event
from sqlalchemy.engine import Connection

from app.db.session import engine
from app.services import event_cache
from app.services.event_cache import EventMeta, get_event_meta

N = 20


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

    event.listen(engine.sync_engine, "before_cursor_execute", _on_execute)
    try:
        yield seen
    finally:
        event.remove(engine.sync_engine, "before_cursor_execute", _on_execute)


async def _burst(redis: Redis, event_id: int) -> list[EventMeta | None]:
    """N 個併發呼叫。呼叫端不再需要 session:重算的 session 由 event_cache 自己開。"""
    return await asyncio.gather(
        *(get_event_meta(redis, event_id=event_id) for _ in range(N))
    )


# ── 第 1 層:process 內 singleflight ──────────────────────────────────────────

@pytest.mark.asyncio
async def test_cold_key_burst_loads_db_once(redis, published_event):
    """冷 key 的 N 個併發 miss 只讓 DB 重算一次,大家拿到同一份,lock 用完就放。"""
    with count_statements("FROM events") as loads:
        metas = await _burst(redis, published_event.id)
    assert len(loads) == 1, f"{len(loads)} DB loads for one cold key"
    assert all(m is not None and m.event_id == published_event.id for m in metas)
    assert await redis.get(event_cache._lock_key(published_event.id)) is None


@pytest.mark.asyncio
async def test_warm_key_burst_never_touches_db(redis, published_event):
    """暖 key 的 burst 完全不碰 DB —— 問題只在冷 key,hit path 沒事。"""
    await get_event_meta(redis, event_id=published_event.id)               # 暖 key

    with count_statements("FROM events") as loads:
        await _burst(redis, published_event.id)
    assert loads == []


@pytest.mark.asyncio
async def test_cancelled_waiters_do_not_abort_the_fill(redis, published_event):
    """一半的等待者被取消(RequestTimeoutMiddleware 砍 request 的情境):剩下的
    照樣拿到值,DB 仍只讀一次,lock 沒有變成孤兒。"""
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


# ── 第 2 層:跨 process 的 Redis lock ─────────────────────────────────────────

@pytest.mark.asyncio
async def test_follower_process_waits_for_the_fill_instead_of_loading(redis, published_event):
    """別的 process 正持有 lock:我們不讀 DB、輪詢等它回填,而且一根毛都不動它的 lock。"""
    key = event_cache._key(published_event.id)
    lock = event_cache._lock_key(published_event.id)
    await redis.set(lock, "other-process", px=event_cache._LOCK_TTL_MS)

    with count_statements("FROM events") as loads:
        waiter = asyncio.create_task(get_event_meta(redis, event_id=published_event.id))
        await asyncio.sleep(event_cache._LOCK_POLL_SECONDS * 3)
        assert not waiter.done()                                          # 還在等,沒自己去讀

        await redis.set(key, event_cache._encode(published_event, {}), ex=60)   # 「別人」回填了
        meta = await asyncio.wait_for(waiter, timeout=1)

    assert loads == []
    assert meta is not None and meta.status.value == "published"
    assert await redis.get(lock) == "other-process"


@pytest.mark.asyncio
async def test_double_check_after_acquiring_the_lock_skips_the_load(redis, published_event, monkeypatch):
    """前一個 leader 剛做完(回填 + 釋放)的瞬間我們拿到 lock:要再看一次快取,
    不能直接讀 DB —— 少了 double-check,「剛做完」會變成多讀一次。"""
    monkeypatch.setattr(event_cache, "_LOCK_POLL_SECONDS", 0.2)   # 把它睡的窗拉大,好在裡面動手
    key = event_cache._key(published_event.id)
    lock = event_cache._lock_key(published_event.id)
    await redis.set(lock, "previous-leader", px=event_cache._LOCK_TTL_MS)

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


@pytest.mark.asyncio
async def test_dead_lock_holder_degrades_to_own_load_after_the_wait(redis, published_event, monkeypatch):
    """holder 死了、lock 還在、永遠不會有人回填:等到上限就自己讀,絕不讓請求失敗。"""
    monkeypatch.setattr(event_cache, "_LOCK_WAIT_SECONDS", 0.1)
    lock = event_cache._lock_key(published_event.id)
    await redis.set(lock, "dead-process", px=10_000)                      # 比等待上限長很多

    with count_statements("FROM events") as loads:
        meta = await asyncio.wait_for(
            get_event_meta(redis, event_id=published_event.id), timeout=2
        )
    assert meta is not None
    assert len(loads) == 1
    assert await redis.get(event_cache._key(published_event.id)) is not None   # 還是回填了


@pytest.mark.asyncio
async def test_lock_is_released_only_by_its_owner(redis):
    """fencing:token 不對就不刪。leader 超時後刪到別人的 lock,會放第三個 loader 進場。"""
    lock = event_cache._lock_key(999)
    await redis.set(lock, "theirs", px=10_000)

    assert await event_cache._release_lock(redis, lock, "mine") is False
    assert await redis.get(lock) == "theirs"

    assert await event_cache._release_lock(redis, lock, "theirs") is True
    assert await redis.get(lock) is None


# ── 邊界 ──────────────────────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_unknown_event_is_none_and_not_cached(redis):
    """不存在的 event 回 None,而且**今天不做 negative cache** —— 每次都會穿透到 DB。
    這條釘住現況,讓之後加 negative cache 是一個明確的決定而不是順手的副作用。"""
    with count_statements("FROM events") as loads:
        assert await get_event_meta(redis, event_id=987_654) is None
    assert len(loads) == 1
    assert await redis.get(event_cache._key(987_654)) is None
    assert await redis.get(event_cache._lock_key(987_654)) is None
