"""Audit log producer — fire events to Redis Stream.

The stream is consumed by ARQ worker which batches them into Postgres.
"""
import json
from datetime import datetime, timezone
from typing import Any

from redis.asyncio import Redis as RedisClient


AUDIT_STREAM_KEY = "audit:events"
AUDIT_STREAM_MAX_LEN = 100_000


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