"""結構化日誌與追蹤 id 的行為測試。

這一層的價值全部在「出事的時候查得到」,所以測試也照這個切:
  1. 欄位真的長出來(JSON、level、context 注入、traceback)
  2. context **一定會還原** —— 洩漏到下一筆是這裡唯一真正危險的 bug
  3. trace_id 真的跨得過 process 邊界(API 綁的那個,消費者接得回來)
"""
import ast
import io
import json
import logging
from pathlib import Path
from uuid import uuid4

import pytest

from app.core.logging import (
    _ContextFilter,
    alert,
    configure_logging,
    current_trace_id,
    log_context,
    new_trace_id,
)
from app.api.middleware import RESPONSE_HEADER
from app.core.security import get_password_hash
from app.models.user import User
from app.services.inventory import ORDER_STREAM_KEY, reserve_and_enqueue

APP = Path(__file__).resolve().parent.parent / "app"


@pytest.fixture
def json_logs():
    """跑真正的 `configure_logging()`,只把它那個 handler 的輸出接到記憶體。

    刻意**不用 capsys**:pytest 自己的 logging 外掛也在 root logger 上動手腳,
    兩套捕捉疊在一起時「這行到底有沒有被寫出去」會變得看不準 —— 而這組測試要驗的
    正是「有沒有寫出去、欄位長怎樣」。換掉 stream 讓 formatter、filter、level 全部
    維持受測狀態,只有目的地是假的。

    測試結束把 handler 收乾淨:root logger 是全域狀態,留著會汙染後面的測試。
    """
    configure_logging(level="DEBUG", component="test")
    stream = io.StringIO()
    handler = logging.getLogger().handlers[0]
    handler.setStream(stream)

    def read() -> list[dict]:
        return [json.loads(line) for line in stream.getvalue().splitlines() if line]

    yield read
    logging.getLogger().handlers.clear()


# ─ 1. 欄位

def test_log_line_is_json_with_the_fields_alarms_depend_on(json_logs):
    logging.getLogger("t").info("hello", extra={"event": "demo"})

    (line,) = json_logs()
    assert line["message"] == "hello"
    assert line["level"] == "INFO"
    assert line["logger"] == "t"
    assert line["event"] == "demo"
    assert line["component"] == "test"      # 分得出是哪一種 process
    assert line["ts"].endswith("+00:00")    # RFC3339、UTC


def test_context_fields_are_injected_without_being_passed(json_logs):
    """context 綁一次,底下所有 log 自動帶上 —— 這就是不用改十五層簽章的理由。"""
    with log_context(trace_id="abc123", order_id=7):
        logging.getLogger("t").warning("deep inside")

    (line,) = json_logs()
    assert line["trace_id"] == "abc123"
    assert line["order_id"] == 7


def test_explicit_extra_wins_over_context(json_logs):
    with log_context(order_id=1):
        logging.getLogger("t").info("closer wins", extra={"order_id": 2})

    assert json_logs()[0]["order_id"] == 2


def test_exception_logging_keeps_the_traceback(json_logs):
    """舊版 `print(f"...: {exc}")` 只留訊息字串;沒有堆疊等於沒說是哪一行炸的。"""
    try:
        raise ValueError("boom")
    except ValueError:
        logging.getLogger("t").exception("failed", extra={"event": "demo_failed"})

    (line,) = json_logs()
    assert "ValueError: boom" in line["exc_info"]
    assert "test_exception_logging_keeps_the_traceback" in line["exc_info"]


def test_alert_marks_the_line_for_a_human(json_logs):
    alert(logging.getLogger("t"), "no automatic remedy", event="demo_alert", order_id=3)

    (line,) = json_logs()
    assert line["needs_human"] is True      # CloudWatch: { $.needs_human IS TRUE }
    assert line["level"] == "ERROR"
    assert line["order_id"] == 3


def test_no_log_field_collides_with_a_logrecord_attribute():
    """`extra=` 用到 LogRecord 既有的屬性名(module / filename / args …)會直接拋
    KeyError。

    **這條的價值在於它檢查的是跑不到的路徑**:座位結構漂移那幾條 alert 只在事故
    當下才會執行 —— 那正是最不能在寫 log 時炸掉的時刻,而一般測試永遠覆蓋不到。
    保留字清單從一個真的 LogRecord 上讀出來,不用手抄(手抄的清單會隨 Python 版本
    漂掉)。
    """
    reserved = set(
        logging.LogRecord("n", logging.INFO, "p", 1, "m", None, None).__dict__
    ) | {"message", "asctime"}

    used: set[str] = set()
    for path in APP.rglob("*.py"):
        for node in ast.walk(ast.parse(path.read_text(), filename=str(path))):
            if not isinstance(node, ast.Call):
                continue
            if isinstance(node.func, ast.Name) and node.func.id == "alert":
                # alert() 把 **fields 原樣塞進 extra
                used |= {
                    kw.arg for kw in node.keywords
                    if kw.arg not in (None, "event", "exc_info")
                }
            for kw in node.keywords:
                if kw.arg == "extra" and isinstance(kw.value, ast.Dict):
                    used |= {
                        key.value for key in kw.value.keys
                        if isinstance(key, ast.Constant) and isinstance(key.value, str)
                    }

    assert not (used & reserved), (
        f"這些 log 欄位會跟 LogRecord 內建屬性撞名並在寫 log 時拋 KeyError:"
        f"{sorted(used & reserved)}"
    )


# ─ 2. 還原(這一組才是真正危險的那個 bug)

def test_context_is_restored_on_exit():
    record = logging.LogRecord("t", logging.INFO, __file__, 1, "m", None, None)

    with log_context(trace_id="inner"):
        assert current_trace_id() == "inner"
    assert current_trace_id() is None

    _ContextFilter().filter(record)
    assert not hasattr(record, "trace_id")


def test_sibling_contexts_do_not_leak_into_each_other(json_logs):
    """**消費者迴圈的那個 bug。**

    一批 500 筆 entry 在同一個 Task 上依序處理。第二筆若沒有自己的 trace_id
    (升級前就躺在 stream 裡的舊 intent),絕不能沿用上一筆的 —— 那會讓 log 看起來
    完全合理但指向錯的訂單,而那種錯誤幾乎不可能靠讀 log 發現。
    """
    logger = logging.getLogger("t")
    for trace_id in ("first", "-"):
        with log_context(trace_id=trace_id):
            logger.info("entry")

    assert [line["trace_id"] for line in json_logs()] == ["first", "-"]


def test_nested_context_merges_then_unwinds():
    with log_context(trace_id="outer"):
        with log_context(order_id=1):
            assert current_trace_id() == "outer"      # 外層的欄位仍在
        assert current_trace_id() == "outer"
    assert current_trace_id() is None


# ─ 3. HTTP 這一側

@pytest.mark.asyncio
async def test_response_carries_a_trace_id_header(client):
    response = await client.get("/v1/events/")

    assert response.headers[RESPONSE_HEADER]


@pytest.mark.asyncio
async def test_each_request_gets_its_own_id(client):
    first = await client.get("/v1/events/")
    second = await client.get("/v1/events/")

    assert first.headers[RESPONSE_HEADER] != second.headers[RESPONSE_HEADER]


@pytest.mark.asyncio
async def test_access_log_carries_the_same_id_the_client_was_given(client, json_logs):
    """回給客戶端的 id 必須就是請求內部綁的那個。

    這條才是整層的核心主張:使用者回報「我的請求壞了,X-Request-Id 是 abc」,
    那個字串要真的能撈出這次請求底下的所有行。兩邊各自產生一個 id 也會讓上面
    那條 header 測試通過,但客訴就完全查不到東西。
    """
    response = await client.get("/v1/events/")

    (line,) = [entry for entry in json_logs() if entry.get("event") == "http_request"]
    assert line["trace_id"] == response.headers[RESPONSE_HEADER]
    assert line["status"] == 200
    assert line["endpoint"] == "list_published_endpoint"
    assert line["method"] == "GET"
    assert line["duration_ms"] >= 0


@pytest.mark.asyncio
async def test_health_checks_are_not_access_logged(client, json_logs):
    """ALB 每 30 秒探一次 /health,Prometheus 固定抓 /metrics —— 在 CloudWatch 上
    那是按量計費的噪音,而且會把真正的請求淹掉。"""
    await client.get("/health")

    assert not [entry for entry in json_logs() if entry.get("event") == "http_request"]


@pytest.mark.asyncio
async def test_client_supplied_request_id_is_not_adopted(client):
    """客戶端不能指定自己的 trace_id。

    這個 API 直對公網。採信客戶端給的值等於讓任何人宣稱跟別人相同的 id —— 一次
    grep 撈出來的會是好幾個人的請求混在一起,而且看起來完全合理。
    """
    response = await client.get("/v1/events/", headers={RESPONSE_HEADER: "attacker-chosen"})

    assert response.headers[RESPONSE_HEADER] != "attacker-chosen"


# ─ 4. 跨 process(這一層的重點)

@pytest.mark.asyncio
async def test_trace_id_is_carried_into_the_order_stream(redis, published_event):
    """API 綁的 trace_id 必須跟著 order intent 進到 stream。

    contextvar 只活在一個 process 裡,消費者是另一個 process —— 少了這個欄位,
    「下單 → 落帳 → reclaim → 死信」在 log 裡就是四段接不起來的線。
    """
    trace_id = new_trace_id()

    with log_context(trace_id=trace_id):
        await reserve_and_enqueue(
            redis,
            event_id=published_event.id,
            user_id=1,
            quantity=1,
            total_price_cents=1500,
            idempotency_key=str(uuid4()),
        )

    (_, fields), = await redis.xrange(ORDER_STREAM_KEY)
    assert fields["trace_id"] == trace_id


@pytest.mark.asyncio
async def test_consumer_logs_under_the_trace_id_from_the_stream(
    db, redis, published_event, drain_orders, json_logs
):
    """消費者要把 stream 欄位裡的 trace_id **掛回自己的 context**。

    只是把它印出來一次是不夠的:目標是「同一個 id 能撈出兩個 process 的行」,
    所以落帳過程中的每一行都得帶上它,而那只有綁進 context 才辦得到。
    """
    user = User(username="tracer", hashed_password=get_password_hash("secret123"))
    db.add(user)
    await db.commit()

    trace_id = new_trace_id()
    with log_context(trace_id=trace_id):
        await reserve_and_enqueue(
            redis,
            event_id=published_event.id,
            user_id=user.id,
            quantity=1,
            total_price_cents=1500,
            idempotency_key=str(uuid4()),
        )

    await drain_orders()      # 落帳那一行是 DEBUG,fixture 已經把層級開到 DEBUG

    persisted = [
        line for line in json_logs() if line.get("event") == "intent_persisted"
    ]
    assert persisted, "消費者沒有記下落帳這件事"
    assert persisted[0]["trace_id"] == trace_id, "消費者的 log 沒有接回 API 的 trace_id"
