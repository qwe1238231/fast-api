"""Audit log producer — fire events to Redis Stream.

The stream is consumed by ARQ worker which batches them into Postgres.
"""
import json
from datetime import datetime, timezone
from typing import Any

from redis.asyncio import Redis as RedisClient


AUDIT_STREAM_KEY = "audit:events"
AUDIT_STREAM_MAX_LEN = 100_000

#: 「哪個欄位從什麼變成什麼」—— 後台編輯類稽核事件的 payload 形狀。
#: 空 dict = 什麼都沒變,所以可以直接當「有沒有改到東西」的條件用,不必再回一個
#: 平行的 bool(兩個回傳值遲早會不一致,而不一致的那個會是稽核 —— 沒有人會發現
#: 它少記了一個欄位)。
#: 值不必自己轉字串:emit_event 用 json.dumps(default=str),datetime 直接吃得下。
type FieldDiff = dict[str, dict[str, Any]]


async def emit_event(
        redis: RedisClient,
        *,
        event_type: str,
        actor_user_id: int | None = None,
        actor_ip: str | None = None,
        target_type: str | None = None,
        target_id: str | None = None,
        payload: dict[str, Any] | None = None,
        success: bool = True,
        error_code: str | None = None,
) -> None:
    """Append an audit event to Redis Stream.
    
    Fast (~1ms). Caller should not await the surrounding code on this.
    Use asyncio.create_task() to fire-and-forget.
    """
    fields = {
        "event_type": event_type,
        "actor_user_id": str(actor_user_id) if actor_user_id is not None else "",
        "actor_ip": actor_ip or "" ,
        "target_type": target_type or "",
        "target_id": target_id or "",
        "payload": json.dumps(payload or {}, default=str),
        "success": "1" if success else "0",
        "error_code": error_code or "",
        "created_at": datetime.now(timezone.utc).isoformat(),
    }

    await redis.xadd(
        AUDIT_STREAM_KEY,
        fields,
        maxlen=AUDIT_STREAM_MAX_LEN,
        approximate=True,
    )