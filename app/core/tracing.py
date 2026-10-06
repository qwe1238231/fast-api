"""OpenTelemetry 追蹤 —— span 的來源、匯出、與既有 trace_id 的接軌。

結構化日誌(core/logging.py)已經讓同一個請求的 log 共用一個 trace_id,但那只回答
「有哪些事發生」,回答不了「時間花在哪」:一筆 POST /orders 的 480ms 裡,
verify_admission 的 Redis 往返幾 ms、配位的 read-compute-CAS 幾 ms、XADD 幾 ms、
consumer 多久才撿到、INSERT 幾 ms —— 只有自己寫了 log 的地方才量得到,而且得拿
時間戳相減。span 把每一段操作(每句 SQL、每個 Redis 指令、每次打 Stripe)變成有起迄
時間的節點,組成一棵樹,畫出來就是瀑布圖。

四個決定:

1. **log 的 trace_id 就是 OTel 的 trace id。** 兩者同為 32 位 hex。HTTP 進來時 server
   span 已經是 current(OTel 的 ASGI middleware 包在整個 stack 外面),TraceIdMiddleware
   直接拿它的 id 當 log 的 trace_id;沒有有效 span 時才退回 uuid4。於是 log 與 span 是
   同一把 key:從瀑布圖點一下能跳到那筆請求的 log,反過來也行。

2. **入站的 trace context 一律不接續。** 全域 propagator 是「只注入、不提取」:對外送
   的 header 照 W3C 寫 traceparent,但從來不從入站請求接別人的 trace。理由同
   api/middleware.py 的「trace_id 一律自己產」—— 這個 API 直接對公網,任何人都能送一個
   traceparent 讓自己的請求掛在別人的 trace 底下。我們自己的通道(Redis Stream)要接續
   時,顯式用 W3C_PROPAGATOR 提取,信任邊界是一個明確的決定,不是預設。

3. **沒設 endpoint 就不裝 exporter,但 provider 照設。** span 仍然在 process 內產生,所以
   trace id 的接軌、跨 process 的傳播在測試與本機都是活的,只是沒有人收。設了
   OTEL_EXPORTER_OTLP_ENDPOINT 才開 OTLP/HTTP 匯出。選 HTTP 不選 gRPC,是為了不把
   grpcio 拖進映像(映像瘦身是另一條待辦,別先往反方向走)。

4. **只用自動 instrumentation,不在業務程式碼裡手開 span。** FastAPI / SQLAlchemy /
   redis / httpx 的 instrumentor 覆蓋了這個系統所有的 I/O 邊界。手開的 span 等真的有
   「某段純計算太慢」的問題再加,不先猜。

core/ 不 import FastAPI(分層規則),所以 FastAPI 那一支 instrumentor 住在 main.py。
"""
import logging
import os
from collections.abc import Mapping
from typing import Any

from opentelemetry import propagate, trace
from opentelemetry.context import Context, get_current
from opentelemetry.instrumentation.httpx import HTTPXClientInstrumentor
from opentelemetry.instrumentation.redis import RedisInstrumentor
from opentelemetry.instrumentation.sqlalchemy import SQLAlchemyInstrumentor
from opentelemetry.propagators.textmap import (
    CarrierT, Getter, Setter, TextMapPropagator, default_getter, default_setter,
)
from opentelemetry.sdk.resources import SERVICE_NAME, Resource
from opentelemetry.sdk.trace import TracerProvider
from opentelemetry.sdk.trace.export import BatchSpanProcessor
from opentelemetry.sdk.trace.sampling import (
    ALWAYS_ON, Decision, ParentBased, Sampler, SamplingResult,
)
from opentelemetry.trace import SpanKind
from opentelemetry.trace.propagation.tracecontext import TraceContextTextMapPropagator

logger = logging.getLogger(__name__)

#: W3C traceparent 的讀寫。**只給我們自己的通道**(Redis Stream 的 producer / consumer)
#: 顯式使用;入站 HTTP 走的是下面那個只注入不提取的全域 propagator。
W3C_PROPAGATOR = TraceContextTextMapPropagator()

_configured = False


class _InjectOnlyPropagator(TextMapPropagator):
    """只注入、不提取(決定 2)。extract 回傳呼叫端給的 context 原樣,等於「沒有父 span」,
    所以每個入站請求的 server span 都是新的 root。"""

    def extract(
        self,
        carrier: CarrierT,
        context: Context | None = None,
        getter: Getter[CarrierT] = default_getter,
    ) -> Context:
        return context if context is not None else Context()

    def inject(
        self,
        carrier: CarrierT,
        context: Context | None = None,
        setter: Setter[CarrierT] = default_setter,
    ) -> None:
        W3C_PROPAGATOR.inject(carrier, context, setter)

    @property
    def fields(self) -> set[str]:
        return W3C_PROPAGATOR.fields


class _NoOrphanClientRoots(Sampler):
    """根節點若是 CLIENT span(沒有父 span 的 Redis / SQL / HTTP 呼叫)就不收。

    這種 span 不是「一個請求」,是背景輪詢:arq 每半秒 ZRANGEBYSCORE 看有沒有 job、
    consumer loop 的 XREADGROUP 阻塞等待、SSE subscriber 的 pubsub —— 每一個都會變成一條
    只有一個 span 的 trace,一小時幾千條,把真正的 trace 淹掉(本地 Tempo 第一次搜
    ticket-worker,前五筆全是 ZRANGEBYSCORE)。有父 span 的 I/O(請求、cron、intent 底下
    的)照收。只管 root:子 span 由 ParentBased 跟著父節點的決定走。
    """

    def should_sample(
        self, parent_context, trace_id, name, kind=None, attributes=None, links=None,
        trace_state=None,
    ) -> SamplingResult:
        if kind is SpanKind.CLIENT:
            return SamplingResult(Decision.DROP)
        return ALWAYS_ON.should_sample(
            parent_context, trace_id, name, kind, attributes, links, trace_state
        )

    def get_description(self) -> str:
        return "NoOrphanClientRoots"


def current_trace_id() -> str | None:
    """當前 span 的 trace id(32 位 hex);沒有有效 span 時回 None。

    給 log 的 trace_id 接軌用(api/middleware.py)。純讀 log 的程式碼不需要它。
    """
    ctx = trace.get_current_span().get_span_context()
    return format(ctx.trace_id, "032x") if ctx.is_valid else None


def current_traceparent() -> str:
    """當前 span 的 W3C traceparent 字串;沒有有效 span 時是空字串。

    給 Redis Stream 的 producer 用:Redis 的欄位值只能是字串,所以這裡直接回字串而不是
    dict carrier,呼叫端照 trace_id 的做法把它當一個欄位帶走。跨 process 的手動搬運跟
    HTTP 的 traceparent header 是同一件事的兩種載體(core/logging.py 的說法)。
    """
    carrier: dict[str, str] = {}
    W3C_PROPAGATOR.inject(carrier)
    return carrier.get("traceparent", "")


def extract_trace_context(carrier: Mapping[str, Any]) -> Context:
    """從一個 dict carrier(Redis Stream 的欄位)讀回 traceparent,給 consumer 接續
    producer 的 trace 用。

    沒有或壞掉的 traceparent → 回**當前** context,不是空 context:升級前就躺在 stream
    裡的舊 intent 沒有這個欄位,它們的 span 就掛在當下的 cron span / consumer loop 底下,
    而不是各自變成一棵孤兒樹。
    """
    return W3C_PROPAGATOR.extract(carrier, context=get_current())


def configure_tracing(*, component: str | None = None) -> None:
    """設 TracerProvider、(有 endpoint 時)掛 OTLP exporter、instrument SQLAlchemy / redis / httpx。

    每個進入點各呼叫一次(API 的 lifespan、worker 的 startup、consumer 的 main),跟
    configure_logging 同一個位置。**冪等**:全域 tracer provider 只能設一次(SDK 對第二次
    set 會警告並忽略),而測試的 session fixture 跟 app 都可能呼叫。

    `component` 預設讀 APP_COMPONENT —— 跟 log 的 component、Postgres 的 application_name
    是同一個環境變數:「這是哪一種 process」只有一個宣告點,三邊看到的名字才對得起來。
    """
    global _configured
    if _configured:
        return
    _configured = True

    # 延遲 import:core/config 會讀 .env 並驗證一堆密鑰,不該在 import 這個模組時就被
    # 拖進來(理由同 configure_logging)。
    from app.core.config import get_settings

    settings = get_settings()
    name = component or os.getenv("APP_COMPONENT", "ticket-api")
    provider = TracerProvider(
        resource=Resource.create({SERVICE_NAME: name}),
        # root 由 _NoOrphanClientRoots 決定(CLIENT root 丟掉、其他全收),子 span 跟父節點。
        sampler=ParentBased(root=_NoOrphanClientRoots()),
    )
    endpoint = settings.OTEL_EXPORTER_OTLP_ENDPOINT
    if endpoint:
        from opentelemetry.exporter.otlp.proto.http.trace_exporter import OTLPSpanExporter

        # 位址由 Settings 交給 exporter,不靠 SDK 自己讀環境變數:.env 裡的值 SDK 看不到,
        # 而「設了卻沒送出去」的失敗方式是完全無聲的。
        provider.add_span_processor(
            BatchSpanProcessor(OTLPSpanExporter(endpoint=f"{endpoint.rstrip('/')}/v1/traces"))
        )
    trace.set_tracer_provider(provider)
    propagate.set_global_textmap(_InjectOnlyPropagator())

    # 引擎在 db/session.py import 時就建好了;instrumentor 不傳 engine 只會包到**之後**
    # 建的引擎,所以要把兩個既有引擎(主 pool 與 cache 的 bulkhead pool)顯式交給它。
    # async engine 要用底下的 sync_engine。instrumentor 是單例,第二次 instrument() 會被
    # 忽略 —— 所以用 engines=[...] 一次給齊,不能呼叫兩次。
    from app.db.session import cache_engine, engine

    SQLAlchemyInstrumentor().instrument(engines=[engine.sync_engine, cache_engine.sync_engine])
    RedisInstrumentor().instrument()
    HTTPXClientInstrumentor().instrument()
    logger.info(
        "tracing configured",
        extra={"event": "tracing_configured", "service_name": name, "export": bool(endpoint)},
    )
