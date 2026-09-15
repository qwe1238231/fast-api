"""Singleflight:同一個 key 的併發計算只跑一次,大家共用結果。

這是 **process 內**的請求合併(request coalescing),不是快取:它不記住結果,只在
「此刻有人正在算同一個 key」時讓後來者搭便車。第一個呼叫者發起計算,計算進行中
到達的所有呼叫者等同一個結果;計算結束後登記項就消失,下一個呼叫者會重新發起。

主要用途是擋 cache stampede 的 process 內那一半:快取 miss 時,同一個 process
裡 N 個併發請求只有一個真的去讀 DB,其餘 N-1 個等它。跨 process 的那一半由呼叫端
用 Redis lock 處理(見 services/event_cache)—— 兩層疊起來的相乘效果是,每個
process 只剩**一個人**去搶 lock、去輪詢,輪詢成本的上限是 process 數而不是請求數。

**取消語意是這個類別存在的理由,不是附帶細節:**

- 計算跑在**獨立的 task** 上,不在發起者的 coroutine 裡。發起者通常是一個 HTTP
  request,而 RequestTimeoutMiddleware 超時會取消它。若計算跑在發起者身上,發起者
  一被取消,所有搭便車的人就永遠等不到。獨立 task 讓「誰發起」與「誰執行」脫鉤。
- 所有等待者(含發起者)都透過 `asyncio.shield` 等。任一等待者被取消只影響它自己,
  計算繼續跑完 —— 就算所有等待者都走了也讓它跑完:快做完的工作丟掉才是浪費,
  結果(例如回填的快取)下一個人會用到。
- 計算拋例外 → 所有等待者拿到同一個例外,登記項移除,下一個呼叫者重新發起。
  不會有人卡在一個已經失敗的 flight 上。

零框架依賴、零 I/O,純 asyncio —— 所以放 core/。
"""
import asyncio
import functools
from collections.abc import Callable, Coroutine, Hashable
from typing import Any


class SingleFlight[K: Hashable, T]:
    """每個 key 同一時間最多一個進行中的計算。

    一個實例通常是模組層級的單例,對應一種資源(例如 event meta)。key 的型別由
    使用端決定(event_id、字串 key 都可以),只要可雜湊。
    """

    def __init__(self) -> None:
        self._inflight: dict[K, asyncio.Task[T]] = {}

    def __len__(self) -> int:
        """進行中的 key 數。給測試與 metrics 用,不是給業務邏輯用。"""
        return len(self._inflight)

    async def run(self, key: K, compute: Callable[[], Coroutine[Any, Any, T]]) -> T:
        """`key` 沒有進行中的計算就以 `compute()` 發起一個;有就等它。回傳計算結果。

        `compute` 是零參數的 coroutine 函式而不是 coroutine 物件:只有真的要發起時
        才呼叫它,搭便車的人不會產生一個沒人 await 的 coroutine(那會被 asyncio
        警告 "coroutine was never awaited")。
        """
        task = self._inflight.get(key)
        if task is None or task.done():
            # done() 的檢查補一個小窗:task 完成到 done callback 執行之間隔了一個
            # 事件迴圈迭代,這期間登記項還在。此時到達的呼叫者若沾上一個已經以例外
            # 結束的 flight,拿到的會是別人的失敗而不是自己的一次重試。
            task = asyncio.create_task(compute(), name=f"singleflight:{key!r}")
            self._inflight[key] = task
            task.add_done_callback(functools.partial(self._forget, key))
        # shield:等待者被取消時,只有這個 await 收到 CancelledError,task 不受影響。
        # shield 也會在外層被取消時把內層的例外標記為「已取回」,所以就算所有等待者
        # 都走了、計算最後又失敗,也不會噴 "Task exception was never retrieved"。
        return await asyncio.shield(task)

    def _forget(self, key: K, task: asyncio.Task[T]) -> None:
        # 只在登記的還是**這個** task 時才刪。done callback 是排程後才跑的,期間
        # `run` 可能已經因為 done() 檢查替同一個 key 登記了新 task —— 無條件刪會把
        # 新的 flight 踢掉,下一個呼叫者又會發起第三個,合併就破了。
        if self._inflight.get(key) is task:
            del self._inflight[key]
