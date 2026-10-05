"""HTTP 這一層的護欄:追蹤 id、請求體上限、每請求逾時。

三個都是裸 ASGI middleware。**不用 `BaseHTTPMiddleware`** 是因為等候室的 SSE 一條
連線最長活 300 秒,而那一層會把回應包進另一個 Task 用 queue 轉送 —— 串流與客戶端
中途斷線在它下面的行為會變得難以推理。裸 ASGI 只是把 `receive` / `send` 包一層,
對串流是透明的。

CORS 用 Starlette 內建的那個(見 `main.py` 的組裝順序)。這裡不重寫一份。

這是 `core/logging.py` 的 FastAPI 轉接層(core/ 不 import FastAPI)。
"""
import asyncio
import json
import logging
import time
from collections.abc import Sequence
from typing import Any

from starlette.applications import Starlette
from starlette.datastructures import Headers, MutableHeaders
from starlette.middleware.cors import CORSMiddleware
from starlette.requests import Request
from starlette.types import ASGIApp, Message, Receive, Scope, Send

from app.api.deps import client_ip
from app.core.config import get_settings
from app.core.logging import log_context, new_trace_id

logger = logging.getLogger(__name__)


async def _send_json(send: Send, *, status: int, detail: str) -> None:
    """從裸 ASGI 送一個 `{"detail": ...}` 的錯誤回應。

    body 形狀跟 `exception_handlers.py` 一致 —— 客戶端不該因為「這個錯誤是在
    middleware 產生的」而需要處理第二種錯誤格式。
    """
    body = json.dumps({"detail": detail}).encode()
    await send({
        "type": "http.response.start",
        "status": status,
        "headers": [
            (b"content-type", b"application/json"),
            (b"content-length", str(len(body)).encode()),
        ],
    })
    await send({"type": "http.response.body", "body": body})

#: 回給客戶端的 header。使用者回報問題時可以直接引用這個值,客服就不必靠時間戳
#: 猜是哪一筆請求。
RESPONSE_HEADER = "X-Request-Id"

#: ALB 自己塞的追蹤 id。收下來(消毒過)當**附加**欄位,用來把 ALB 的 access log
#: 跟應用的 log 接起來 —— 那是唯一能回答「請求到底有沒有進到應用」的證據。
_ALB_TRACE_HEADER = "x-amzn-trace-id"
_ALB_TRACE_MAX_LEN = 64

#: 不寫 access log 的路徑。ALB 每 30 秒探一次 `/health`、Prometheus 固定抓
#: `/metrics` —— 這些是噪音,而且在 CloudWatch 上是按量計費的噪音。
_ACCESS_LOG_EXCLUDED = frozenset({"/health", "/health/deps", "/metrics"})


def _alb_trace_id(scope: Scope) -> str | None:
    """ALB 的 `X-Amzn-Trace-Id`,消毒後回傳;不在代理後面時一律忽略。

    為什麼要消毒:ALB 會**保留**客戶端自己送來的 Root 欄位,所以這個 header 不是
    完全可信的 —— 沒有長度上限的話,它就是一條把任意內容塞進我們 log 的路。截短
    並且只留可列印 ASCII,讓它退化成「一個沒用的字串」而不是「一個注入點」。

    只在 `TRUSTED_PROXY_COUNT > 0`(確實有我們自己的代理在前面)時才讀,跟
    `deps.client_ip` 用同一個信任判準:前面沒有代理時,這個 header 純粹是客戶端
    輸入,收下來沒有任何價值。
    """
    if get_settings().TRUSTED_PROXY_COUNT <= 0:
        return None
    raw = Headers(scope=scope).get(_ALB_TRACE_HEADER)
    if not raw:
        return None
    cleaned = "".join(ch for ch in raw[:_ALB_TRACE_MAX_LEN] if ch.isprintable())
    return cleaned or None


class TraceIdMiddleware:
    """每個 HTTP 請求綁一個 trace_id,並在請求結束時發一行結構化的 access log。

    **為什麼是裸 ASGI middleware 而不是 `BaseHTTPMiddleware`**:等候室的 SSE 端點
    一條連線最長活 300 秒,而 `BaseHTTPMiddleware` 會把回應包進另一個 Task 用
    queue 轉送 —— 串流與客戶端中途斷線在那層下面的行為會變得難以推理。裸 ASGI
    只是把 `send` 包一層,對串流是透明的。

    **trace_id 一律自己產,不接受客戶端指定。** 這個 API 直接對公網,客戶端可控的
    值當關聯鍵有兩個問題:任何人都能宣稱跟別人相同的 id(於是一次 grep 撈出來的是
    好幾個人的請求混在一起,而且看起來完全合理),以及長度/字元不受控。目前也沒有
    上游服務需要延續既有的 trace —— 真的有了(內部服務互打)再來談要信任誰,那時
    信任邊界是一個要明確做的決定,不是預設。
    """

    def __init__(
        self,
        app: ASGIApp,
        *,
        excluded_paths: frozenset[str] = _ACCESS_LOG_EXCLUDED,
    ) -> None:
        self.app = app
        self.excluded_paths = excluded_paths

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return

        trace_id = new_trace_id()
        fields: dict[str, Any] = {"trace_id": trace_id}
        alb_trace_id = _alb_trace_id(scope)
        if alb_trace_id is not None:
            fields["alb_trace_id"] = alb_trace_id

        status_code: int | None = None

        async def send_wrapper(message: Message) -> None:
            nonlocal status_code
            if message["type"] == "http.response.start":
                status_code = message["status"]
                MutableHeaders(scope=message).append(RESPONSE_HEADER, trace_id)
            await send(message)

        started = time.perf_counter()
        with log_context(**fields):
            try:
                await self.app(scope, receive, send_wrapper)
            except Exception:
                # 這裡記,是因為外層的 ServerErrorMiddleware 在我們的 context
                # **之外** —— 它那行 traceback 沒有 trace_id,而一個接不回請求的
                # traceback 正是最難用的那種。代價是同一個例外會有兩行,可接受。
                logger.exception(
                    "unhandled exception",
                    extra={"event": "request_failed", "path": scope["path"]},
                )
                raise
            finally:
                self._log_access(scope, status_code, time.perf_counter() - started)

    def _log_access(
        self, scope: Scope, status_code: int | None, elapsed_seconds: float
    ) -> None:
        if scope["path"] in self.excluded_paths:
            return
        endpoint = scope.get("endpoint")
        logger.info(
            "http request",
            extra={
                "event": "http_request",
                "method": scope["method"],
                # 原始路徑(不是路由模板)。log 不是 metric,沒有 cardinality 問題,
                # 而「哪一筆訂單」正是查問題時要的東西。要分組時用 endpoint。
                "path": scope["path"],
                "endpoint": getattr(endpoint, "__name__", None),
                # 例外時 http.response.start 從未送出 → 沒有狀態碼可讀;外層會把它
                # 變成 500,所以這裡就記 500,不要留一個 null 讓人以為請求沒結束。
                "status": status_code if status_code is not None else 500,
                "duration_ms": round(elapsed_seconds * 1000, 1),
                "client_ip": client_ip(Request(scope)),
            },
        )


def configure_cors(app: Starlette, origins: Sequence[str]) -> None:
    """掛上 CORS。`origins` 是空的就**什麼都不做**(等同今天的行為:只允許同源)。

    住在這裡而不是 main.py 的組裝區塊,是為了讓下面每一個決定都能被測試 —— 尤其是
    `expose_headers`:少了它前端讀不到那兩個 header,但**請求會照樣成功**,所以沒有
    任何東西會壞掉、只是功能悄悄不見。那種東西一定要有測試釘住。
    """
    if not origins:
        return
    app.add_middleware(
        CORSMiddleware,
        allow_origins=list(origins),
        # cookie(refresh + CSRF 雙送,見 auth.py)要跟著跨來源請求走,所以必須是
        # True —— 也正因為如此,origins 不能是 `*`(Settings 的 validator 會擋:那個
        # 組合在規範上無效,瀏覽器直接拒絕)。
        allow_credentials=True,
        allow_methods=["GET", "POST", "OPTIONS"],
        # 顯式列出而不用 `*`:這份清單就是「前端能送哪些 header」的文件,而 `*` 在
        # credentialed 請求下本來也不被當成通配符。
        allow_headers=[
            "Authorization",
            "Content-Type",
            "Idempotency-Key",
            "Admission-Token",
            "X-CSRF-Token",
        ],
        # **這兩個一定要 expose。** 跨來源時瀏覽器只讓 JS 讀 safelisted 的回應 header:
        #   X-Request-Id —— 客訴要引用的那個 id;讀不到的話追蹤那一層只做完一半。
        #   Retry-After  —— 429/503 的退避秒數;讀不到前端只能瞎猜重試間隔。
        expose_headers=[RESPONSE_HEADER, "Retry-After"],
    )


class _BodyTooLarge(Exception):
    """內部訊號:請求體超過上限。只在 BodySizeLimitMiddleware 內部傳遞。"""


class BodySizeLimitMiddleware:
    """拒絕過大的請求體。

    **為什麼需要**:`request.body()`(webhook 用它驗簽)與 Pydantic 的解析都會把整個
    body 讀進記憶體,而 uvicorn/h11 沒有任何 body 大小上限。0.25 vCPU / 512 MB 的
    Fargate 任務上,幾個併發的大 body 就能把它推去 OOM —— 而那不需要通過認證、不需要
    等候室的入場券,任何人對任何端點都做得到。它是目前最便宜的一種打法。

    兩道檢查,因為只有第一道會被繞過:
      1. `Content-Length` —— 正常客戶端都會送,可以在**讀任何一個 byte 之前**就拒絕。
      2. 邊讀邊數 —— chunked 傳輸沒有 Content-Length,只能一邊收一邊算。

    超過時的例外從 `receive` 冒出來、穿過 handler、在這裡被接住換成 413。**不能在
    receive 裡直接回傳截斷的 body**:那會讓 handler 看到一個「合法但不完整」的請求,
    症狀變成 422(格式錯誤)—— 對客戶端是誤導,對我們是查不出原因的客訴。
    """

    def __init__(self, app: ASGIApp, *, max_bytes: int) -> None:
        self.app = app
        self.max_bytes = max_bytes

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return

        declared = Headers(scope=scope).get("content-length")
        if declared is not None:
            try:
                if int(declared) > self.max_bytes:
                    await self._reject(scope, send, declared=int(declared))
                    return
            except ValueError:
                pass          # 壞掉的 Content-Length → 交給下面邊讀邊數那道

        received = 0

        async def receive_counting() -> Message:
            nonlocal received
            message = await receive()
            if message["type"] == "http.request":
                received += len(message.get("body", b""))
                if received > self.max_bytes:
                    raise _BodyTooLarge
            return message

        try:
            await self.app(scope, receive_counting, send)
        except _BodyTooLarge:
            await self._reject(scope, send, declared=None)

    async def _reject(self, scope: Scope, send: Send, *, declared: int | None) -> None:
        logger.warning(
            "request body too large",
            extra={
                "event": "body_too_large",
                "path": scope["path"],
                "limit_bytes": self.max_bytes,
                # 宣告值 vs 實際數到的:前者是「客戶端自己說多大」,後者是 chunked
                # 情況下我們數到的。分開記,才看得出是哪一道擋下來的。
                "declared_bytes": declared,
            },
        )
        await _send_json(
            send,
            status=413,
            detail=f"request body exceeds {self.max_bytes} bytes",
        )


class RequestTimeoutMiddleware:
    """給每個請求一個產生回應的時限。

    **為什麼需要**:API 是單一 uvicorn worker(0.25 vCPU),沒有時限的話一個卡住的
    請求就佔著一條事件迴圈上的協程與一條 DB 連線,而 `/health` 刻意不碰依賴,所以
    ALB 會繼續把流量送進來 —— 服務對外表現成「活著但全部很慢」。

    **時限是到「回應開始」為止,不是到「回應結束」。** 這一點對這個專案是必要的:
    等候室的 SSE 一條連線設計上要活 300 秒,用整體耗時當判準會把它整個功能砍掉。
    語意上也才對 —— 「handler 必須及時給出回應」是合理要求,「串流必須及時結束」不是。
    做法是收到 `http.response.start` 就把計時器關掉(`reschedule(None)`)。

    附帶效果:慢速上傳(slow loris)也被這個時限蓋住 —— handler 等 body 的時間算在
    裡面,因為那發生在 `http.response.start` 之前。

    數值必須**小於 ALB 的 idle timeout(預設 60 秒)**,否則先放棄的是 ALB:客戶端拿到
    的是 ALB 產生的 504,上面沒有 trace_id,而我們的 log 裡什麼都沒有。
    """

    def __init__(self, app: ASGIApp, *, timeout_seconds: float) -> None:
        self.app = app
        self.timeout_seconds = timeout_seconds

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return

        started = time.perf_counter()
        response_started = False

        try:
            async with asyncio.timeout(self.timeout_seconds) as timer:

                async def send_wrapper(message: Message) -> None:
                    nonlocal response_started
                    if message["type"] == "http.response.start":
                        response_started = True
                        timer.reschedule(None)   # 回應已開始 → 剩下的串流不再受限
                    await send(message)

                await self.app(scope, receive, send_wrapper)
        except TimeoutError:
            # 逾時只可能發生在 response.start 之前(之後計時器已關),所以這裡一定
            # 還沒送出任何東西 —— 可以安全地自己回一個 504。
            logger.warning(
                "request timed out before producing a response",
                extra={
                    "event": "request_timeout",
                    "path": scope["path"],
                    "method": scope["method"],
                    "timeout_seconds": self.timeout_seconds,
                    "duration_ms": round((time.perf_counter() - started) * 1000, 1),
                },
            )
            if not response_started:
                await _send_json(send, status=504, detail="request timed out")
