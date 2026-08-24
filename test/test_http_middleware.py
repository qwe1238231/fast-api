"""HTTP 護欄:CORS、請求體上限、每請求逾時,以及它們的組裝順序。

三個護欄的失敗模式都是「安靜」的:
  - CORS 少了 expose_headers → 請求照樣成功,只是前端讀不到 X-Request-Id。
  - body 上限沒生效 → 一切正常,直到有人拿大 body 把 512 MB 的任務打到 OOM。
  - 逾時把串流也算進去 → 等候室的 SSE 每 15 秒被砍一次,而它會自動重連,所以
    看起來只是「有點不穩」。
所以這裡的斷言都很具體。

大部分用**獨立的小 ASGI app** 測,不是打真的 app:上限與逾時的邊界要餵不合法的
輸入才驗得到(1 MiB 的 body、睡 999 秒的 handler),而那些情境在真 app 上不存在。
另外各有一條打真 app 的整合測試,確認「真的掛上去了、而且用的是真的設定值」。
"""
import asyncio

import pytest
from fastapi import FastAPI, Request
from httpx import ASGITransport, AsyncClient
from pydantic import ValidationError
from starlette.responses import StreamingResponse

from app.api.middleware import (
    RESPONSE_HEADER,
    BodySizeLimitMiddleware,
    RequestTimeoutMiddleware,
    configure_cors,
)
from app.core.config import Settings, get_settings
from app.main import app as real_app


def _client(app: FastAPI) -> AsyncClient:
    return AsyncClient(transport=ASGITransport(app=app), base_url="https://test")


# ─ 組裝順序

def test_middleware_order_is_inside_out():
    """由外而內必須是 trace → CORS → body 上限 → 逾時 → metrics。

    `user_middleware` 的順序就是由外而內。每一層的位置都有具體理由(見 main.py),
    而排錯的症狀會指向完全錯誤的方向 —— 例如 CORS 排到 body 上限**裡面**時,413 的
    回應不帶 CORS header,瀏覽器就把它顯示成 CORS 錯誤而不是 413。
    """
    names = [m.cls.__name__ for m in real_app.user_middleware]

    assert names[0] == "TraceIdMiddleware", "trace_id 必須在最外層"
    # CORS 預設是關的(沒有前端),所以它不在清單裡 —— 但只要它在,就必須在
    # body 上限之外。
    if "CORSMiddleware" in names:
        assert names.index("CORSMiddleware") < names.index("BodySizeLimitMiddleware")
    assert names.index("BodySizeLimitMiddleware") < names.index(
        "RequestTimeoutMiddleware"
    )
    assert names.index("RequestTimeoutMiddleware") < names.index(
        "PrometheusInstrumentatorMiddleware"
    ), "逾時的請求也要被記進 metrics,所以 instrumentator 必須包在它外面"


# ─ CORS

@pytest.mark.asyncio
async def test_cors_exposes_the_headers_the_frontend_must_read():
    """`X-Request-Id` 與 `Retry-After` 必須 expose,否則跨來源時 JS 讀不到。

    這是最容易被「整理掉」的一行,而拿掉它不會讓任何請求失敗 —— 只會讓客訴再也
    引用不到 id、讓前端只能瞎猜重試間隔。
    """
    app = FastAPI()
    configure_cors(app, ["https://tickets.example"])

    @app.get("/x")
    async def _x() -> dict:
        return {}

    async with _client(app) as client:
        response = await client.get("/x", headers={"Origin": "https://tickets.example"})

    exposed = {
        h.strip().lower()
        for h in response.headers["access-control-expose-headers"].split(",")
    }
    assert RESPONSE_HEADER.lower() in exposed
    assert "retry-after" in exposed
    assert response.headers["access-control-allow-credentials"] == "true"


@pytest.mark.asyncio
async def test_cors_preflight_allows_the_headers_the_api_actually_reads():
    """下單要送 Idempotency-Key 與 Admission-Token,refresh 要送 X-CSRF-Token。
    漏掉任何一個,瀏覽器會在 preflight 就把請求擋掉(伺服器完全不會看到它)。"""
    app = FastAPI()
    configure_cors(app, ["https://tickets.example"])

    async with _client(app) as client:
        response = await client.options(
            "/v1/orders/",
            headers={
                "Origin": "https://tickets.example",
                "Access-Control-Request-Method": "POST",
                "Access-Control-Request-Headers": (
                    "authorization,content-type,idempotency-key,"
                    "admission-token,x-csrf-token"
                ),
            },
        )

    assert response.status_code == 200
    allowed = {
        h.strip().lower()
        for h in response.headers["access-control-allow-headers"].split(",")
    }
    assert {
        "authorization", "content-type", "idempotency-key",
        "admission-token", "x-csrf-token",
    } <= allowed


def test_cors_is_off_by_default():
    """沒有設 origins 就完全不掛 —— 跟今天的行為一致,不是「掛上去但允許所有人」。"""
    app = FastAPI()
    configure_cors(app, [])

    assert not [m for m in app.user_middleware if m.cls.__name__ == "CORSMiddleware"]


def test_wildcard_origin_is_refused_in_production():
    """`*` 配 credentialed 請求在規範上無效(瀏覽器直接拒絕),所以它給的是
    「我開好 CORS 了」的錯覺,實際上前端仍然壞掉 —— 而且順手把來源限制拆了。"""
    base = get_settings().model_dump()

    with pytest.raises(ValidationError, match="explicit origins"):
        Settings(**{**base, "CORS_ALLOW_ORIGINS": "*", "DEBUG": False})


@pytest.mark.parametrize(
    "inner",
    [
        {"DB_STATEMENT_TIMEOUT_MS": 9000},
        {"STRIPE_TIMEOUT_SECONDS": 9.0},
    ],
    ids=["statement_timeout", "stripe_timeout"],
)
def test_inner_timeouts_must_be_shorter_than_the_request_timeout(inner):
    """每一個內層逾時都必須比請求逾時短,否則它等於不存在。

    兩個具體後果:
      - statement_timeout 太長 → 請求被放棄,查詢仍在資料庫裡跑完(連線與 rows 還被佔著)
      - Stripe 逾時太長 → webhook 的去重標記已提交,而退款呼叫被外層砍掉;Stripe 重送
        會被當成處理過,**那筆退款靜靜消失**

    兩者的外觀都是「逾時都設好了」,而且各自的數字都很合理。
    """
    base = get_settings().model_dump()

    with pytest.raises(ValidationError, match="must be less than"):
        Settings(**{**base, "REQUEST_TIMEOUT_SECONDS": 5.0, **inner})


# ─ 請求體上限

def _limited_app(max_bytes: int) -> FastAPI:
    app = FastAPI()

    @app.post("/echo")
    async def _echo(request: Request) -> dict:
        body = await request.body()
        return {"len": len(body)}

    app.add_middleware(BodySizeLimitMiddleware, max_bytes=max_bytes)
    return app


@pytest.mark.asyncio
async def test_body_under_the_limit_passes():
    async with _client(_limited_app(100)) as client:
        response = await client.post("/echo", content=b"x" * 100)

    assert response.status_code == 200
    assert response.json() == {"len": 100}


@pytest.mark.asyncio
async def test_oversized_body_is_rejected_by_content_length():
    """正常客戶端會送 Content-Length,所以這一刀能在**讀任何一個 byte 之前**落下。"""
    async with _client(_limited_app(100)) as client:
        response = await client.post("/echo", content=b"x" * 101)

    assert response.status_code == 413
    assert "exceeds" in response.json()["detail"]


@pytest.mark.asyncio
async def test_oversized_chunked_body_is_rejected_while_reading():
    """chunked 傳輸沒有 Content-Length —— 這是繞過第一道檢查的方式,所以第二道
    (邊讀邊數)必須真的存在。

    回的仍然是 413 而**不是** 422:如果只是在 receive 裡截斷 body,handler 會看到
    一個「合法但不完整」的請求而回格式錯誤 —— 對客戶端是誤導,對我們是查不出原因。
    """
    async def chunks():
        for _ in range(10):
            yield b"x" * 50          # 500 bytes 總量,上限 100

    async with _client(_limited_app(100)) as client:
        response = await client.post("/echo", content=chunks())

    assert response.status_code == 413


@pytest.mark.asyncio
async def test_the_real_app_enforces_its_configured_limit(client):
    """整合:護欄真的掛在真 app 上,而且用的是設定裡那個值。

    刻意打一個**不存在**的路徑:上限在路由之前就生效,所以這裡驗到的是
    「還沒進到任何 handler 就被擋下來」——這正是它要防的事。
    """
    limit = get_settings().MAX_REQUEST_BODY_BYTES

    response = await client.post("/v1/nonexistent", content=b"x" * (limit + 1))

    assert response.status_code == 413
    assert response.headers[RESPONSE_HEADER]      # 仍然帶 trace_id(trace 在最外層)


# ─ 每請求逾時

def _slow_app(timeout_seconds: float, *, sleep_seconds: float) -> FastAPI:
    app = FastAPI()

    @app.get("/slow")
    async def _slow() -> dict:
        await asyncio.sleep(sleep_seconds)
        return {}

    @app.get("/stream")
    async def _stream() -> StreamingResponse:
        async def body():
            yield b"first\n"                     # 立刻送出 → response.start 出去了
            await asyncio.sleep(sleep_seconds)   # 之後串很久 —— 不該被砍
            yield b"last\n"

        return StreamingResponse(body(), media_type="text/plain")

    app.add_middleware(RequestTimeoutMiddleware, timeout_seconds=timeout_seconds)
    return app


@pytest.mark.asyncio
async def test_slow_handler_gets_504():
    async with _client(_slow_app(0.05, sleep_seconds=5)) as client:
        response = await client.get("/slow")

    assert response.status_code == 504
    assert response.json()["detail"] == "request timed out"


@pytest.mark.asyncio
async def test_fast_handler_is_untouched():
    async with _client(_slow_app(5, sleep_seconds=0)) as client:
        response = await client.get("/slow")

    assert response.status_code == 200


@pytest.mark.asyncio
async def test_streaming_response_is_not_killed_by_the_timeout():
    """**這條保護的是等候室。**

    SSE 一條連線設計上活 300 秒。用「整體耗時」當判準會把整個等候室砍掉,而症狀是
    每 N 秒斷線一次、瀏覽器的 EventSource 自動重連 —— 看起來只是「有點不穩」,
    不會有人把它連到這個逾時上。所以時限只算到 `http.response.start`。
    """
    async with _client(_slow_app(0.05, sleep_seconds=0.3)) as client:
        response = await client.get("/stream")

    assert response.status_code == 200
    assert response.text == "first\nlast\n"       # 串流跑完了,沒有被中途砍掉
