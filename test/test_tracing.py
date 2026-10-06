"""OTel 追蹤:log 的 trace_id 與 span 是同一把 key、入站 traceparent 不被接續、
I/O 邊界自動變成子 span。

provider 由 conftest 的 session fixture 設好(沒有 exporter)。這裡掛一個 in-memory
exporter 到同一個 provider 上讀 span —— 加上去就拿不掉(SDK 沒有 remove),所以每條
測試開頭 clear(),而 processor 留著對其他測試無害(只是多存幾個 span 在記憶體)。
"""
from uuid import uuid4

import pytest
from opentelemetry import trace
from opentelemetry.sdk.trace import ReadableSpan
from opentelemetry.sdk.trace.export import SimpleSpanProcessor
from opentelemetry.sdk.trace.export.in_memory_span_exporter import InMemorySpanExporter
from opentelemetry.trace import SpanKind
from sqlalchemy import select

from app.api.middleware import RESPONSE_HEADER
from app.core.logging import current_trace_id as log_trace_id
from app.core.security import create_admission_token
from app.models.user import User
from app.services.inventory import ORDER_STREAM_KEY
from app.worker import ORDER_CONSUMER_GROUP, _consume_batch, cron_job

_exporter: InMemorySpanExporter | None = None


@pytest.fixture
def spans() -> InMemorySpanExporter:
    global _exporter
    if _exporter is None:
        _exporter = InMemorySpanExporter()
        trace.get_tracer_provider().add_span_processor(SimpleSpanProcessor(_exporter))
    _exporter.clear()
    return _exporter


def _server_span(exporter: InMemorySpanExporter) -> ReadableSpan:
    found = [s for s in exporter.get_finished_spans() if s.kind is SpanKind.SERVER]
    assert len(found) == 1, [s.name for s in exporter.get_finished_spans()]
    return found[0]


def _db_system(span: ReadableSpan) -> str | None:
    # semconv 新舊兩代的 key 都認:db.system(舊)/ db.system.name(穩定版)。
    attrs = span.attributes or {}
    return attrs.get("db.system.name") or attrs.get("db.system")


@pytest.mark.asyncio
async def test_the_log_trace_id_is_the_otel_trace_id(client, spans):
    """回應的 X-Request-Id(= log 的 trace_id)必須就是 server span 的 trace id ——
    這是「從瀑布圖跳到 log、從 log 跳到瀑布圖」能成立的唯一前提。"""
    r = await client.get("/v1/events/")
    assert r.status_code == 200
    server = _server_span(spans)
    assert r.headers[RESPONSE_HEADER] == format(server.context.trace_id, "032x")


@pytest.mark.asyncio
async def test_an_inbound_traceparent_is_not_continued(client, spans):
    """公網上任何人都能送 traceparent。接續它等於讓客戶端決定我們的 trace id ——
    跟「trace_id 一律自己產」是同一條政策,由 inject-only propagator 保證。"""
    foreign = "0af7651916cd43dd8448eb211c80319c"
    r = await client.get(
        "/v1/events/", headers={"traceparent": f"00-{foreign}-b7ad6b7169203331-01"}
    )
    assert r.status_code == 200
    server = _server_span(spans)
    assert server.parent is None, "server span 必須是 root,不能掛在別人的 trace 底下"
    assert r.headers[RESPONSE_HEADER] != foreign


@pytest.mark.asyncio
async def test_sql_runs_as_a_child_span_of_the_request(client, spans):
    """SQLAlchemy 的 instrumentor 要包到 db/session.py 在 import 時就建好的引擎 ——
    不顯式傳 engines 的話,這裡一個 SQL span 都不會有,而且沒有任何錯誤。"""
    r = await client.get("/v1/events/")
    assert r.status_code == 200
    server = _server_span(spans)
    sql = [
        s for s in spans.get_finished_spans()
        if s.kind is SpanKind.CLIENT and _db_system(s) == "postgresql"
    ]
    assert sql, [s.name for s in spans.get_finished_spans()]
    assert all(s.context.trace_id == server.context.trace_id for s in sql)


@pytest.mark.asyncio
async def test_redis_commands_become_spans(redis, spans):
    tracer = trace.get_tracer("test")
    with tracer.start_as_current_span("outer") as outer:
        await redis.get("tracing:probe")
    redis_spans = [s for s in spans.get_finished_spans() if s.name == "GET"]
    assert redis_spans, [s.name for s in spans.get_finished_spans()]
    assert redis_spans[0].parent is not None
    assert redis_spans[0].parent.span_id == outer.get_span_context().span_id


@pytest.mark.asyncio
async def test_background_io_without_a_parent_is_not_a_trace(redis, spans):
    """arq 的輪詢、consumer loop 的阻塞 XREADGROUP 這種沒有父 span 的 I/O 不該各自變成
    一條單 span 的 trace(本地 Tempo 第一次搜 worker,前五筆全是 ZRANGEBYSCORE)。
    有父 span 時照收 —— 上一條測試驗的就是那一半。"""
    await redis.get("tracing:orphan")                 # 沒有任何 current span
    assert not [s for s in spans.get_finished_spans() if s.name == "GET"]


# ─ 跨 process:Redis Stream 與 cron

async def _ensure_group(redis) -> None:
    try:
        await redis.xgroup_create(ORDER_STREAM_KEY, ORDER_CONSUMER_GROUP, id="0", mkstream=True)
    except Exception as exc:
        if "BUSYGROUP" not in str(exc):
            raise


@pytest.mark.asyncio
async def test_an_order_intent_is_processed_inside_the_requests_trace(
    client, db, redis, published_event, spans, drain_orders
):
    """XADD 帶 traceparent,consumer 撿到時接回同一棵樹:下單的 server span 與落帳的
    CONSUMER span 是同一個 trace,而且後者的父節點就是前者 —— 跨 process 的瀑布圖靠
    這個成立。log 那邊的 trace_id 欄位照舊,值與 traceparent 裡的 trace id 相同。"""
    await client.post("/v1/users/", json={"username": "alice", "password": "secret123"})
    token = (await client.post(
        "/v1/auth/token", data={"username": "alice", "password": "secret123"}
    )).json()["access_token"]
    uid = await db.scalar(select(User.id).where(User.username == "alice"))

    spans.clear()
    r = await client.post(
        "/v1/orders/",
        json={"event_id": published_event.id, "quantity": 1},
        headers={
            "Authorization": f"Bearer {token}",
            "Idempotency-Key": str(uuid4()),
            "Admission-Token": create_admission_token(
                user_id=uid, event_id=published_event.id, ttl_seconds=120
            ),
        },
    )
    assert r.status_code == 202, r.text
    request = _server_span(spans)
    trace_hex = format(request.context.trace_id, "032x")

    _, fields = (await redis.xrange(ORDER_STREAM_KEY))[-1]
    assert trace_hex in fields["traceparent"], fields
    assert fields["trace_id"] == trace_hex == r.headers[RESPONSE_HEADER]

    await drain_orders()
    consumer = [s for s in spans.get_finished_spans() if s.kind is SpanKind.CONSUMER]
    assert len(consumer) == 1, [s.name for s in spans.get_finished_spans()]
    assert consumer[0].context.trace_id == request.context.trace_id
    assert consumer[0].parent.span_id == request.context.span_id
    inserts = [
        s for s in spans.get_finished_spans()
        if _db_system(s) == "postgresql" and s.name.startswith("INSERT")
        and s.context.trace_id == request.context.trace_id
    ]
    assert inserts, "落帳的 INSERT 要掛在同一棵樹上"
    assert all(s.parent.span_id == consumer[0].context.span_id for s in inserts)


@pytest.mark.asyncio
async def test_a_legacy_intent_without_traceparent_hangs_off_the_current_span(redis, spans):
    """升級前就躺在 stream 裡的 intent 沒有 traceparent:它的 span 掛在當前 context
    (consumer loop / cron span)底下,不是各自一棵孤兒樹。"""
    await _ensure_group(redis)
    await redis.xadd(ORDER_STREAM_KEY, {
        "user_id": "999999", "event_id": "999999", "quantity": "1",
        "total_price_cents": "0", "idempotency_key": str(uuid4()), "zone_id": "",
        "trace_id": "legacy",
    })
    tracer = trace.get_tracer("test")
    with tracer.start_as_current_span("loop") as loop:
        await _consume_batch(redis)          # FK 會擋下這筆,但 span 照開
    consumer = [s for s in spans.get_finished_spans() if s.kind is SpanKind.CONSUMER]
    assert len(consumer) == 1
    assert consumer[0].parent.span_id == loop.get_span_context().span_id


@pytest.mark.asyncio
async def test_a_cron_run_is_a_root_span_and_its_logs_carry_that_trace_id(spans):
    seen: dict[str, str | None] = {}

    @cron_job
    async def probe(ctx: dict) -> None:
        seen["log"] = log_trace_id()
        seen["span"] = format(trace.get_current_span().get_span_context().trace_id, "032x")

    await probe({})
    cron = next(s for s in spans.get_finished_spans() if s.name == "cron probe")
    assert cron.parent is None
    assert seen["log"] == seen["span"] == format(cron.context.trace_id, "032x")
