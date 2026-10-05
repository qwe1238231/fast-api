"""結構化日誌 —— process 內用 contextvar 自動帶,跨 process 靠訊息欄位手動搬。

三件事在這裡定義,而且**只**在這裡定義:

  1. **綁定** —— `log_context(...)` 把欄位掛進當前 asyncio Task 的 context。同一個
     Task 底下所有的 log 自動帶上它們,不必把 trace_id 當參數穿過十五層函式簽章。
     那正是 contextvars 存在的理由;少了它,「加一個關聯 id」會變成改動幾十個簽章。
  2. **注入** —— `_ContextFilter` 在**每次 emit 的當下**把 context 讀出來寫進 record。
     所以呼叫端只要照常 `logger.info(...)`,完全不必知道 context 的存在。
  3. **格式** —— JSON。CloudWatch 的 metric filter 因此可以比對**欄位**
     (`{ $.event = "inventory_drift" }`)而不是比對散文。舊的過濾器比對的是
     `"INVENTORY DRIFT event"` 這種訊息字串 —— 任何人改一下措辭,告警就失效,
     而失效的方式是「再也不響」,不會有任何錯誤。

**contextvar 只活在一個 process 裡。** 它就是進程記憶體裡的變數,Redis Stream 那一
端拿不到 —— 所以跨邊界必須把 id 序列化進訊息(見 `services/inventory.py` 的 XADD
與 `worker._consume_batch`)。這跟 HTTP 用 `traceparent` header 跨服務是同一件事的
兩種載體。

**core/ 不 import FastAPI**(專案分層規則),所以這裡只有標準函式庫。HTTP 那一側的
middleware 住在 `app/api/middleware.py`。
"""
import logging
import logging.config
import os
from collections.abc import Iterator
from contextlib import contextmanager
from contextvars import ContextVar
from typing import Any
from uuid import uuid4

#: 當前 context 綁定的欄位。**永遠整份換掉,不要原地改**:預設值 `{}` 是所有
#: context 共用的同一個物件,原地 mutate 會漏到別的請求上,而症狀是「log 看起來
#: 完全合理但指向錯的訂單」—— 幾乎不可能靠讀 log 發現。
_context: ContextVar[dict[str, Any]] = ContextVar("log_context", default={})

#: 追蹤 id 的欄位名。多處引用(middleware、stream 欄位、worker),所以只宣告一次。
TRACE_ID_FIELD = "trace_id"


def new_trace_id() -> str:
    """產生一個新的追蹤 id。

    自己產、不接受客戶端指定 —— 理由見 `api/middleware.py` 的 `TraceIdMiddleware`。
    """
    return uuid4().hex


def current_trace_id() -> str | None:
    """當前 context 的追蹤 id;沒有綁定時回 None。

    給「要把 id 搬到另一個 process」的地方用(例如把它寫進 Redis Stream 的欄位)。
    純讀 log 的程式碼不需要它 —— filter 會自動注入。
    """
    value = _context.get().get(TRACE_ID_FIELD)
    return value if isinstance(value, str) else None


@contextmanager
def log_context(**fields: Any) -> Iterator[None]:
    """在這個區塊內,所有 log 都自動帶上 `fields`(與外層已綁定的欄位合併)。

    **刻意只提供 context manager 而不是裸的 set/reset。** 消費者是一個
    `while True` 迴圈,一批 500 筆 entry 在同一個 Task 上依序處理 —— 忘記 reset
    的話,上一筆的 trace_id 會洩漏到下一筆(尤其是升級前就躺在 stream 裡、沒有
    trace_id 欄位的舊 intent,它會直接冒充成前一筆)。用 `with` 讓「一定會還原」
    變成結構保證,而不是靠寫的人記得。
    """
    merged = {**_context.get(), **fields}
    token = _context.set(merged)
    try:
        yield
    finally:
        _context.reset(token)


class _ContextFilter(logging.Filter):
    """把 context 的欄位寫進 LogRecord。掛在 handler 上,對所有 logger 生效。

    已經存在的屬性不覆蓋 —— 呼叫端顯式傳的 `extra=` 優先於 context,因為那是比較
    近的、比較具體的資訊。
    """

    def filter(self, record: logging.LogRecord) -> bool:
        for key, value in _context.get().items():
            if not hasattr(record, key):
                setattr(record, key, value)
        return True


def alert(
    logger: logging.Logger,
    message: str,
    *,
    event: str,
    exc_info: bool = False,
    **fields: Any,
) -> None:
    """記一筆「**沒有自動修復路徑、需要人來看**」的事件。

    這些各自都罕見到不值得一條專屬告警,但「其中任何一個發生了」是絕對要知道的 ——
    所以它們共用一個 `needs_human: true` 欄位,CloudWatch 上就是一條
    `{ $.needs_human IS TRUE }` 的過濾器(見 infra/monitoring.tf)。

    做成函式而不是「記得在 extra 裡加 needs_human」:那個判準是**一個決定**,不該
    散落在十幾個呼叫點上,不然下一條新的 ALERT 一定會忘記加,然後靜靜躺在 log 裡
    等人去 grep。
    """
    logger.error(
        message,
        exc_info=exc_info,
        extra={"event": event, "needs_human": True, **fields},
    )


def configure_logging(*, level: str | None = None, component: str | None = None) -> None:
    """把 root logger 換成「JSON + context 注入」的單一 handler。

    每個進入點(API 的 lifespan、order consumer 的 main、ARQ worker 的 startup)各呼叫
    一次。`dictConfig` 是**取代**而不是附加,所以重複呼叫不會疊出兩份 handler。

    **呼叫時機**:uvicorn 與 arq 都會先設好自己的 logging 才把控制權交出來,所以這裡
    必須在它們之後跑(lifespan / on_startup),不能在 import 時 —— 那會被它們蓋掉。

    `component` 預設讀 `APP_COMPONENT`,跟 `db/session.py` 拿去當 Postgres
    `application_name` 的是同一個環境變數:同一件事(這是哪一種 process)只有一個
    宣告點,查 log 跟查 `pg_stat_activity` 看到的名字才會對得起來。
    """
    # 延遲 import:`core/config` 會讀 .env 並驗證一堆密鑰,不該在 import 這個模組
    # 時就被拖進來(測試裡有只想用 log_context 而不碰設定的情境)。
    from app.core.config import get_settings

    settings_level = level or get_settings().LOG_LEVEL
    logging.config.dictConfig({
        "version": 1,
        # uvicorn / arq / sqlalchemy 的 logger 在這之前就建好了,不要停用它們 ——
        # 停用等於把它們的輸出整段丟掉。
        "disable_existing_loggers": False,
        "filters": {"context": {"()": _ContextFilter}},
        "formatters": {
            "json": {
                "()": "pythonjsonlogger.json.JsonFormatter",
                # 只列非時間欄位;時間交給 timestamp= 產生 RFC3339(UTC、含微秒),
                # 比 asctime 的 "2026-08-17 15:10:42,498" 好對時。
                "format": "%(levelname)s %(name)s %(message)s",
                "rename_fields": {"levelname": "level", "name": "logger"},
                "static_fields": {
                    "component": component or os.getenv("APP_COMPONENT", "ticket-api"),
                },
                "timestamp": "ts",
            },
        },
        "handlers": {
            "stdout": {
                "class": "logging.StreamHandler",
                "stream": "ext://sys.stdout",
                "formatter": "json",
                "filters": ["context"],
            },
        },
        "root": {"handlers": ["stdout"], "level": settings_level},
        "loggers": {
            # uvicorn 自己裝了 handler 且 propagate=False。清掉它的 handler 並打開
            # propagate,它的輸出才會走我們這條 JSON 管線 —— 否則 CloudWatch 裡會是
            # 一半 JSON、一半純文字,而任何以欄位為基礎的過濾器都只看得到一半。
            "uvicorn": {"handlers": [], "propagate": True},
            "uvicorn.error": {"handlers": [], "propagate": True},
            # access log 由 TraceIdMiddleware 自己發(uvicorn 的那條在我們的 context
            # 之外,拿不到 trace_id,而沒有 trace_id 的 access log 正是最沒用的一種)。
            # 留著只會變成同一個請求兩行。
            "uvicorn.access": {"handlers": [], "propagate": False},
            "arq": {"handlers": [], "propagate": True},
        },
    })
