"""稽核管線的耐久性:事件不能靜默消失。

這條路徑的特別之處在於**失敗沒有訊號**。訂單掉了會有人來問「我的票呢」;稽核事件
掉了,事後查帳看到的是一段沒有任何異常的空白 —— 而稽核的全部價值就是它可信。

舊版有三個漏點,這裡一個一個釘住:
  1. 沒有 XAUTOCLAIM —— commit 之後 xack 之前掛掉,那批留在 PEL 永遠不再被讀
     (`>` 只給新訊息)
  2. 整批一個交易 —— 一顆毒藥拖垮幾千筆好資料,而且下一輪重讀同一批再賠一次
  3. 消費速率追不上時,XADD 的 approximate trim 會靜靜丟掉最舊的事件
"""
import json
from datetime import datetime, timezone

import pytest
from sqlalchemy import func, select

from app.core.config import get_settings
from app.models.audit_log import AuditLog
from app.services.audit import AUDIT_STREAM_KEY, emit_event
from app.worker import (
    AUDIT_CONSUMER_GROUP,
    AUDIT_CONSUMER_NAME,
    AUDIT_DEAD_LETTER_KEY,
    _reclaim_audit_entries,
    consume_audit_events,
    ensure_consumer_group,
)

pytestmark = pytest.mark.asyncio


async def _group(redis):
    await ensure_consumer_group(redis, AUDIT_STREAM_KEY, AUDIT_CONSUMER_GROUP)


async def _count(db) -> int:
    return await db.scalar(select(func.count()).select_from(AuditLog))


async def _emit(redis, **kwargs) -> None:
    await emit_event(redis, event_type="auth.login_success", **kwargs)


async def _raw_add(redis, fields: dict) -> str:
    """繞過 emit_event 直接塞 —— 用來造出 emit_event 產不出的壞資料。"""
    return await redis.xadd(AUDIT_STREAM_KEY, fields)


# ─ 快樂路徑

async def test_events_reach_postgres(db, redis) -> None:
    await _group(redis)
    for i in range(5):
        await _emit(redis, actor_ip=f"10.0.0.{i}", target_type="user", target_id=str(i))

    await consume_audit_events({"redis_client": redis})
    assert await _count(db) == 5


async def test_a_single_run_drains_more_than_one_batch(db, redis) -> None:
    """舊版一輪只讀一批就收工,上限是每分鐘一萬筆。追不上的話 XADD 的
    approximate trim 會把最舊的事件直接丟掉 —— 而那不會有任何錯誤。

    這裡用小量驗證「會一直讀到抽乾」這個行為本身,不是驗吞吐量。
    """
    await _group(redis)
    for _ in range(25):
        await _emit(redis)

    await consume_audit_events({"redis_client": redis})
    assert await _count(db) == 25


# ─ 漏點 1:PEL 回收

async def test_entries_stuck_in_the_pending_list_are_reclaimed(db, redis) -> None:
    """模擬「讀了、寫了、但還沒 ack 就掛掉」。

    XREADGROUP 之後不 ack,那筆就留在 PEL。舊版下一輪用 `>` 讀只會拿到新訊息,
    這一筆從此人間蒸發 —— 而且 Redis 那邊看起來一切正常。
    """
    await _group(redis)
    await _emit(redis, target_id="stuck")

    # 讀走但不 ack —— 消費者在這一刻掛掉。
    read = await redis.xreadgroup(
        groupname=AUDIT_CONSUMER_GROUP, consumername=AUDIT_CONSUMER_NAME,
        streams={AUDIT_STREAM_KEY: ">"}, count=10,
    )
    assert read and len(read[0][1]) == 1
    assert await _count(db) == 0

    # 一般消費完全看不到它(`>` 只給新訊息)。
    await consume_audit_events({"redis_client": redis})
    # reclaim 的 idle 門檻是 60 秒,所以剛剛那筆還不該被搶走。
    assert await _count(db) == 0

    # min_idle_ms=0 模擬「已經閒置夠久」。
    reclaimed = await _reclaim_audit_entries(redis, min_idle_ms=0, max_deliveries=5)
    assert reclaimed == 1
    assert await _count(db) == 1


async def test_the_cron_actually_calls_reclaim(db, redis, monkeypatch) -> None:
    """**測接線,不只測零件。**

    上面那條測的是 _reclaim_audit_entries 本身會動;這條測的是 consume_audit_events
    真的有呼叫它。少了這一條,把 cron 裡那幾行刪掉整個檔案還是綠的 —— 而症狀就是
    卡在 PEL 的事件又開始靜默消失,跟修好之前一模一樣。
    """
    await _group(redis)
    await _emit(redis, target_id="stuck")
    await redis.xreadgroup(
        groupname=AUDIT_CONSUMER_GROUP, consumername=AUDIT_CONSUMER_NAME,
        streams={AUDIT_STREAM_KEY: ">"}, count=10,
    )
    assert await _count(db) == 0

    monkeypatch.setattr(get_settings(), "AUDIT_RECLAIM_IDLE_MS", 0)
    await consume_audit_events({"redis_client": redis})
    assert await _count(db) == 1


async def test_reclaim_respects_the_idle_threshold(db, redis) -> None:
    """還在處理中的 entry 不能被搶走 —— 否則兩個消費者會同時寫同一筆。"""
    await _group(redis)
    await _emit(redis)
    await redis.xreadgroup(
        groupname=AUDIT_CONSUMER_GROUP, consumername=AUDIT_CONSUMER_NAME,
        streams={AUDIT_STREAM_KEY: ">"}, count=10,
    )
    assert await _reclaim_audit_entries(redis, min_idle_ms=60_000, max_deliveries=5) == 0
    assert await _count(db) == 0


async def test_poison_entries_are_dead_lettered_not_dropped(db, redis) -> None:
    """投遞次數用完就放棄,但**搬到死信串流**而不是丟掉。

    稽核跟訂單不一樣:訂單死信要退座位、標記 claim 失敗,有補償動作可做;稽核沒有
    東西可補償,唯一該做的就是別讓它無聲消失。
    """
    await _group(redis)
    entry_id = await _raw_add(redis, {"event_type": "x" * 200, "success": "1"})
    await redis.xreadgroup(
        groupname=AUDIT_CONSUMER_GROUP, consumername=AUDIT_CONSUMER_NAME,
        streams={AUDIT_STREAM_KEY: ">"}, count=10,
    )

    # max_deliveries=1:已經投遞過一次,這輪直接死信。
    await _reclaim_audit_entries(redis, min_idle_ms=0, max_deliveries=1)

    dead = await redis.xrange(AUDIT_DEAD_LETTER_KEY)
    assert len(dead) == 1
    assert dead[0][1]["_original_id"] == entry_id
    assert await _count(db) == 0
    # 已經 ack,不會再卡在 PEL 裡無限重試。
    assert await redis.xpending(AUDIT_STREAM_KEY, AUDIT_CONSUMER_GROUP) == {
        "pending": 0, "min": None, "max": None, "consumers": [],
    }


# ─ 漏點 2:一顆毒藥不能拖垮整批

async def test_one_bad_entry_does_not_lose_the_good_ones(db, redis) -> None:
    """整批一個交易是為了吞吐,但一批裡有一顆毒藥就會整個回滾。

    舊版在這裡會把整批好資料一起賠掉,而且下一輪重讀同一批、再賠一次 —— 一筆爛
    資料可以讓整條稽核管線永久停擺。
    """
    await _group(redis)
    for i in range(3):
        await _emit(redis, target_id=f"good{i}")
    # payload 不是合法 JSON —— emit_event 產不出這種,但重放、手動塞、或未來改壞
    # 序列化都會。
    await _raw_add(redis, {"event_type": "auth.login_success", "payload": "{not json",
                           "success": "1"})
    for i in range(3):
        await _emit(redis, target_id=f"after{i}")

    await consume_audit_events({"redis_client": redis})

    assert await _count(db) == 6            # 六筆好的全部落帳
    dead = await redis.xrange(AUDIT_DEAD_LETTER_KEY)
    assert len(dead) == 1                   # 壞的那筆進死信
    assert dead[0][1]["_dead_reason"] == "malformed"


async def test_a_malformed_entry_is_not_silently_coerced(db, redis) -> None:
    """缺 event_type 的 entry 不能被當成空字串寫進去。

    靜默補值會產生一列「看起來正常但內容是編的」稽核紀錄 —— 那比少一列更糟:
    少一列查得出來,編出來的查不出來。
    """
    await _group(redis)
    await _raw_add(redis, {"actor_ip": "10.0.0.1", "success": "1"})

    await consume_audit_events({"redis_client": redis})

    assert await _count(db) == 0
    assert len(await redis.xrange(AUDIT_DEAD_LETTER_KEY)) == 1


# ─ 落帳的內容要對

async def test_the_persisted_row_carries_everything(db, redis) -> None:
    await _group(redis)
    now = datetime.now(timezone.utc)
    await emit_event(
        redis, event_type="event.updated", actor_user_id=None, actor_ip="10.1.2.3",
        target_type="event", target_id="42", payload={"changes": {"name": "x"}},
        success=False, error_code="CONFLICT",
    )
    await consume_audit_events({"redis_client": redis})

    row = await db.scalar(select(AuditLog))
    assert row.event_type == "event.updated"
    assert row.actor_ip == "10.1.2.3"
    assert row.target_type == "event" and row.target_id == "42"
    assert row.payload == {"changes": {"name": "x"}}
    assert row.success is False
    assert row.error_code == "CONFLICT"
    # created_at 是**事件發生時間**(emit 當下),不是寫進 Postgres 的時間 ——
    # 分區路由與事後查帳都靠它。
    assert abs((row.created_at - now).total_seconds()) < 5


# ─ 冪等性的邊界:重複優於遺失

async def test_reclaim_may_duplicate_and_that_is_the_deliberate_trade(db, redis) -> None:
    """commit 成功、ack 失敗的那批會被重放,於是 Postgres 出現重複的列。

    這是刻意的取捨:稽核紀錄重複是雜訊,消失是失去證據而且沒有任何訊號。要根除
    重複得為每一列加 stream_id 的唯一索引,而那在這張寫入密集又已分區的表上不
    划算(唯一索引還必須包含分區鍵)。這條測試把這個行為釘住,免得之後有人看到
    重複就以為是 bug。
    """
    await _group(redis)
    await _emit(redis, target_id="once")
    await consume_audit_events({"redis_client": redis})
    assert await _count(db) == 1

    # 手動把它放回 PEL:重讀 + 不 ack。
    await redis.xadd(AUDIT_STREAM_KEY, {"event_type": "auth.login_success",
                                        "success": "1", "payload": "{}"})
    await redis.xreadgroup(
        groupname=AUDIT_CONSUMER_GROUP, consumername=AUDIT_CONSUMER_NAME,
        streams={AUDIT_STREAM_KEY: ">"}, count=10,
    )
    await _reclaim_audit_entries(redis, min_idle_ms=0, max_deliveries=5)
    assert await _count(db) == 2


# ─ 積壓要叫

def _events(caplog) -> set[str]:
    """把結構化 log 的 event 欄位收集起來 —— 它在 extra 裡,不在訊息文字裡。"""
    return {getattr(rec, "event", None) for rec in caplog.records}


async def test_hitting_the_per_run_cap_alerts(db, redis, monkeypatch, caplog) -> None:
    """一個安靜的上限讀起來就像「全部處理完了」。

    實際上積壓正在長大,而上游的 XADD 會用 approximate trim 把最舊的事件丟掉 ——
    所以撞到上限必須是一條 needs_human 的 ALERT,不是靜靜收工。
    """
    await _group(redis)
    for _ in range(5):
        await _emit(redis)

    monkeypatch.setattr("app.worker.AUDIT_BATCH", 1)
    monkeypatch.setattr("app.worker.AUDIT_MAX_BATCHES", 2)
    with caplog.at_level("WARNING"):
        await consume_audit_events({"redis_client": redis})

    assert await _count(db) == 2                     # 只搬得動兩批
    assert "audit_consumer_capped" in _events(caplog)


async def test_lag_above_the_threshold_alerts(db, redis, monkeypatch, caplog) -> None:
    """積壓沒有別的偵測手段:事件是被 Redis 靜靜修掉的,Postgres 端看不出少了什麼。

    也不能用 XLEN 判斷 —— 稽核事件 ack 之後不 XDEL(靠 maxlen 汰換),所以 XLEN
    穩定之後永遠接近上限,跟消費進度無關。要看的是消費者群組的 lag。
    """
    await _group(redis)
    for _ in range(5):
        await _emit(redis)

    monkeypatch.setattr("app.worker.AUDIT_BATCH", 1)
    monkeypatch.setattr("app.worker.AUDIT_MAX_BATCHES", 1)
    monkeypatch.setattr(get_settings(), "AUDIT_LAG_WARN", 1)
    with caplog.at_level("WARNING"):
        await consume_audit_events({"redis_client": redis})

    assert "audit_lag_high" in _events(caplog)


async def test_no_alert_when_the_stream_is_drained(db, redis, monkeypatch, caplog) -> None:
    """對照組。少了它,「永遠都在叫」跟「叫得對」看起來一樣。"""
    await _group(redis)
    for _ in range(5):
        await _emit(redis)

    monkeypatch.setattr(get_settings(), "AUDIT_LAG_WARN", 0)
    with caplog.at_level("WARNING"):
        await consume_audit_events({"redis_client": redis})

    assert await _count(db) == 5
    assert "audit_lag_high" not in _events(caplog)
    assert "audit_consumer_capped" not in _events(caplog)
