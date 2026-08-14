import asyncio
from contextlib import asynccontextmanager

from fastapi import FastAPI
from fastapi.responses import JSONResponse, RedirectResponse
from sqlalchemy import text

from app.api.deps import DbSession, Redis
from app.core.config import get_settings
from app.core.redis import create_redis_client
from app.services.queue_events import run_subscriber
from app.services.stripe_client import create_stripe_client
from app.api.v1.router import api_router
from app.api.exception_handlers import register_exception_handlers

#: 每一項依賴探測的逾時。比 ALB 的 timeout(5 秒)短,這樣即使有人誤把 ALB 指到
#: /health/deps,探測也會先回答而不是讓 ALB 判定逾時 —— 逾時看起來跟「網路壞了」
#: 一樣,而我們想知道的是「哪一個依賴壞了」。
_DEPS_PROBE_TIMEOUT_SECONDS = 2.0

from prometheus_fastapi_instrumentator import Instrumentator
from prometheus_client import REGISTRY
from app.core.queue_metrics import QueueDepthCollector


@asynccontextmanager
async def lifespan(app: FastAPI):
    settings = get_settings()
    app.state.redis = create_redis_client(settings.REDIS_URL)
    app.state.stripe, app.state.stripe_http = create_stripe_client(settings.STRIPE_SECRET_KEY)
    subscriber = asyncio.create_task(run_subscriber(app.state.redis))   # waiting-room pokes -> SSE mailboxes
    yield
    subscriber.cancel()
    try:
        await subscriber
    except asyncio.CancelledError:
        pass
    await app.state.stripe_http.close_async()
    await app.state.redis.aclose()


app = FastAPI(
    title="Ticket System API",
    version="1.0.0",
    lifespan=lifespan,
)
register_exception_handlers(app)
Instrumentator(
    # regex, matched with re.search -> anchor so "/" doesn't match every path
    excluded_handlers=["^/metrics$", "^/docs$", "^/openapi.json$", "^/$", "^/health"],
).instrument(app).expose(app)
REGISTRY.register(QueueDepthCollector())   # order_stream_backlog / order_dead_letter_depth on /metrics
app.include_router(api_router, prefix="/v1")

@app.get("/", include_in_schema=False)
async def read_root():
    return RedirectResponse(url="/docs")


@app.get("/health", include_in_schema=False)
async def health() -> dict[str, str]:
    """存活探測 —— **ALB 的健康檢查用這一支,而且它刻意不碰任何依賴。**

    以前 ALB 檢查的是 `/` 配 matcher 200-399:`/` 回 307 轉去 /docs,所以「健康」的
    判準其實是「那個轉址還在」。那不是契約,是巧合 —— 有人哪天把根路徑改掉,健康檢查
    的語意就跟著默默改變。

    **為什麼不在這裡查 DB 與 Redis(這是刻意的取捨,不是偷懶):**
    ALB 的健康檢查同時決定兩件事 —— 要不要送流量、以及(接上 ECS 之後)要不要殺掉
    重啟這個任務。DB 是**共用**的,所以資料庫一抖,深度檢查會讓**每一個** target 同時
    變 unhealthy:ALB 沒有健康的後端可以送(對使用者是完全中斷),而 ECS 會把所有任務
    殺掉重啟,那些重啟再回頭捶正在恢復的資料庫。一次依賴抖動被放大成全面停機加驚群。
    任務重啟只治得好「這個 process 自己壞了」,治不好「大家共用的東西壞了」。

    「活著但服務不了」由既有的 `HTTPCode_Target_5XX_Count` 告警負責 —— 那是真實流量
    上的數值訊號,比一個合成探測更有證據力。深度檢查在 `/health/deps`,給人看、以及
    給部署後的煙霧測試用。
    """
    return {"status": "ok"}


@app.get("/health/deps", include_in_schema=False)
async def health_deps(db: DbSession, redis: Redis) -> JSONResponse:
    """依賴探測 —— 真的碰 DB 與 Redis。**ALB 不用這一支**(理由見 `health`)。

    它的消費者是兩個:值班的人(「是我的程式壞了還是資料庫壞了?」)以及 deploy.yml
    在滾完服務之後的煙霧測試。沒有消費者的健康檢查只是裝飾。

    每一項各自逾時:一個卡住的依賴不能讓這支探測自己也卡住 —— 那會讓「壞了」跟
    「還在查」變得無法區分,而那正是健康檢查最不該有的性質。
    """
    async def probe(name: str, coro) -> tuple[str, str]:
        try:
            await asyncio.wait_for(coro, timeout=_DEPS_PROBE_TIMEOUT_SECONDS)
            return name, "ok"
        except Exception as exc:                      # 逾時也算在內
            return name, f"{type(exc).__name__}: {exc}"[:200]

    results = dict(await asyncio.gather(
        probe("postgres", db.execute(text("SELECT 1"))),
        probe("redis", redis.ping()),
    ))
    healthy = all(v == "ok" for v in results.values())
    return JSONResponse(
        status_code=200 if healthy else 503,
        content={"status": "ok" if healthy else "degraded", "checks": results},
    )