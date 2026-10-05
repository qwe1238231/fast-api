"""core/singleflight 的取消語意與合併語意。

純 asyncio,不碰 DB / Redis。每個測試用 `asyncio.Event` 當閘門把計算「卡住」,
讓所有呼叫者都排進同一個 flight 之後才放行 —— 用 sleep 等時序的測試在慢機器上
會亂跳,閘門是確定性的。
"""
import asyncio

import pytest

from app.core.singleflight import SingleFlight

pytestmark = pytest.mark.asyncio

N = 10


class Boom(Exception):
    pass


async def _let_waiters_register() -> None:
    """一個事件迴圈迭代:每個 `run` 呼叫在 create_task / 登記之後才第一次 await,
    所以跑過一輪之後,所有呼叫者都已經掛在同一個 shield 上。"""
    await asyncio.sleep(0)


async def test_concurrent_callers_share_one_computation():
    sf: SingleFlight[str, str] = SingleFlight()
    gate = asyncio.Event()
    calls = 0

    async def compute() -> str:
        nonlocal calls
        calls += 1
        await gate.wait()
        return "v"

    waiters = [asyncio.create_task(sf.run("k", compute)) for _ in range(N)]
    await _let_waiters_register()
    assert len(sf) == 1                                 # N 個呼叫者,1 個 flight

    gate.set()
    assert await asyncio.gather(*waiters) == ["v"] * N
    assert calls == 1

    await asyncio.sleep(0)                              # done callback 在下一輪跑
    assert len(sf) == 0                                 # 結束就忘掉:這不是快取


async def test_sequential_calls_recompute():
    """flight 結束後再來的呼叫者要重新算 —— 合併的是「同時」,不是「同一個 key」。"""
    sf: SingleFlight[str, int] = SingleFlight()
    calls = 0

    async def compute() -> int:
        nonlocal calls
        calls += 1
        return calls

    assert await sf.run("k", compute) == 1
    assert await sf.run("k", compute) == 2


async def test_distinct_keys_do_not_share():
    sf: SingleFlight[str, str] = SingleFlight()
    gate = asyncio.Event()

    async def compute_for(key: str):
        async def compute() -> str:
            await gate.wait()
            return key
        return compute

    a = asyncio.create_task(sf.run("a", await compute_for("a")))
    b = asyncio.create_task(sf.run("b", await compute_for("b")))
    await _let_waiters_register()
    assert len(sf) == 2

    gate.set()
    assert await asyncio.gather(a, b) == ["a", "b"]


async def test_cancelling_the_initiator_does_not_cancel_the_flight():
    """發起者(第一個呼叫者)被取消,其他等待者仍拿到結果,計算只跑一次。

    這是 RequestTimeoutMiddleware 砍掉 leader request 的情境。"""
    sf: SingleFlight[str, str] = SingleFlight()
    gate = asyncio.Event()
    calls = 0

    async def compute() -> str:
        nonlocal calls
        calls += 1
        await gate.wait()
        return "v"

    initiator = asyncio.create_task(sf.run("k", compute))
    followers = [asyncio.create_task(sf.run("k", compute)) for _ in range(N - 1)]
    await _let_waiters_register()

    initiator.cancel()
    with pytest.raises(asyncio.CancelledError):
        await initiator

    gate.set()
    assert await asyncio.gather(*followers) == ["v"] * (N - 1)
    assert calls == 1


async def test_flight_completes_even_when_every_waiter_is_cancelled():
    """所有等待者都走了,計算還是跑完 —— 副作用(例如回填快取)下一個人會用到。"""
    sf: SingleFlight[str, str] = SingleFlight()
    gate = asyncio.Event()
    finished = asyncio.Event()

    async def compute() -> str:
        await gate.wait()
        finished.set()
        return "v"

    waiters = [asyncio.create_task(sf.run("k", compute)) for _ in range(N)]
    await _let_waiters_register()
    for w in waiters:
        w.cancel()
    results = await asyncio.gather(*waiters, return_exceptions=True)
    assert all(isinstance(r, asyncio.CancelledError) for r in results)

    gate.set()
    await asyncio.wait_for(finished.wait(), timeout=1)  # 沒人等它,它還是完成了

    await asyncio.sleep(0)
    assert len(sf) == 0


async def test_exception_reaches_every_waiter_and_clears_the_key():
    """計算失敗:所有等待者拿到同一個例外;登記項移除;下一個呼叫者重新發起。"""
    sf: SingleFlight[str, str] = SingleFlight()
    gate = asyncio.Event()
    calls = 0

    async def failing() -> str:
        nonlocal calls
        calls += 1
        await gate.wait()
        raise Boom

    waiters = [asyncio.create_task(sf.run("k", failing)) for _ in range(N)]
    await _let_waiters_register()
    gate.set()
    results = await asyncio.gather(*waiters, return_exceptions=True)
    assert all(isinstance(r, Boom) for r in results)
    assert calls == 1

    await asyncio.sleep(0)
    assert len(sf) == 0

    async def ok() -> str:
        return "recovered"

    assert await sf.run("k", ok) == "recovered"       # 沒有人卡在失敗的 flight 上


async def test_late_arrival_after_completion_starts_a_fresh_flight():
    """task 完成到 done callback 執行之間有一個迴圈迭代的窗;此時到達的呼叫者不該
    沾上已結束(尤其是已失敗)的 flight。"""
    sf: SingleFlight[str, str] = SingleFlight()

    async def failing() -> str:
        raise Boom

    first = asyncio.create_task(sf.run("k", failing))
    await _let_waiters_register()                      # failing 的 task 已排程
    # 讓 failing task 跑完但 done callback 還沒跑:再一輪
    await asyncio.sleep(0)
    with pytest.raises(Boom):
        await first

    async def ok() -> str:
        return "fresh"

    # 不論 callback 有沒有跑過,done() 檢查都要讓這次呼叫發起新的 flight
    assert await sf.run("k", ok) == "fresh"
