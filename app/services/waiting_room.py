"""Virtual waiting room — lottery-order admission control, backed by Redis.

register(): add the user to a draw ZSET with a deterministic, unpredictable
  score = hash(event_id, user_id, per-event secret salt).
    - deterministic  -> re-registering can't re-roll for a better spot
    - salt is secret -> users can't predict/grind their score
    - hash is uniform -> fair random order (kills the network-speed advantage)

status(): the user is admitted once their rank in the draw falls below the
  admitted cutoff. The admission controller advances that cutoff at a fixed rate,
  and only after the registration window closes — so arrival time within the
  window is irrelevant (everyone in the window is one batch).
"""
import hashlib
import secrets
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone

import jwt
from jwt.exceptions import InvalidTokenError
from redis.asyncio import Redis

from app.core.config import get_settings
from app.core.exceptions import AdmissionDenied
from app.models.event import Event
from app.services.inventory import get_available
from app.services.queue_events import publish_global_poke


def _draw_key(event_id: int) -> str:
    return f"queue:{event_id}:draw"


def _admit_start_key(event_id: int) -> str:
    return f"queue:{event_id}:admit_start"   # epoch seconds when admission begins (window close)


def _salt_key(event_id: int) -> str:
    return f"queue:{event_id}:salt"


def _paused_key() -> str:
    return "admission:paused"   # global circuit breaker — set by the health monitor


@dataclass(frozen=True)
class QueueState:
    admitted: bool
    people_ahead: int | None = None
    sold_out: bool = False
    paused: bool = False


async def set_admission_paused(redis: Redis, paused: bool, *, ttl_seconds: int = 120) -> None:
    """Circuit breaker toggle. Set with a TTL so admission auto-resumes if the
    monitor stops running (fail-open); cleared promptly when healthy again."""
    if paused:
        await redis.set(_paused_key(), "1", ex=ttl_seconds)
    else:
        await redis.delete(_paused_key())
    await publish_global_poke(redis)   # nudge every waiting SSE connection to re-read status()


def window(event: Event) -> tuple[datetime, datetime]:
    """(opens_at, closes_at) for registration. Explicit columns win; otherwise
    fall back to sale_starts_at minus the configured lead / buffer (behaviour A)."""
    s = get_settings()
    opens = event.queue_opens_at or event.sale_starts_at - timedelta(seconds=s.QUEUE_LEAD_TIME_SECONDS)
    closes = event.queue_closes_at or event.sale_starts_at - timedelta(seconds=s.QUEUE_ADMISSION_BUFFER_SECONDS)
    return opens, closes


async def _ensure_salt(redis: Redis, event_id: int) -> str:
    """First writer sets a random salt; everyone then reads the same one."""
    key = _salt_key(event_id)
    await redis.set(key, secrets.token_hex(16), nx=True)
    return await redis.get(key)


def _score(event_id: int, user_id: int, salt: str) -> float:
    digest = hashlib.sha256(f"{event_id}:{user_id}:{salt}".encode()).digest()
    return float(int.from_bytes(digest[:6], "big"))   # 48 bits -> exact in a float64 ZSET score


async def register(redis: Redis, *, event: Event, user_id: int) -> None:
    """Add the user to the lottery draw. Idempotent and non-re-rollable."""
    salt = await _ensure_salt(redis, event.id)
    await redis.zadd(_draw_key(event.id), {str(user_id): _score(event.id, user_id, salt)}, nx=True)


async def setup(redis: Redis, event: Event) -> None:
    """Prepare the waiting room (call at publish): fix the secret salt and record
    when admission begins (the registration window's close time)."""
    await _ensure_salt(redis, event.id)
    _, closes = window(event)
    await redis.set(_admit_start_key(event.id), closes.timestamp())


async def _admitted_count(redis: Redis, event_id: int) -> int:
    """Admitted-so-far = RATE * seconds since the window closed, capped at the
    number registered. A pure function of the clock — no counter to advance."""
    start = await redis.get(_admit_start_key(event_id))
    if start is None:
        return 0
    elapsed = datetime.now(timezone.utc).timestamp() - float(start)
    if elapsed <= 0:
        return 0
    total = await redis.zcard(_draw_key(event_id))
    return min(int(elapsed * get_settings().QUEUE_ADMISSION_RATE), total)


async def status(redis: Redis, *, event_id: int, user_id: int) -> QueueState:
    """Current waiting-room state for a user.

    Two resilience short-circuits stop admission (waiters keep their place):
      - sold_out: inventory hit 0 — nothing to buy, so don't feed people into a
        dead sale (real-time; recovers if released seats bring stock back).
      - paused: the circuit breaker is open (downstream unhealthy).
    """
    sold_out = await get_available(redis, event_id=event_id) <= 0
    paused = bool(await redis.exists(_paused_key()))
    rank = await redis.zrank(_draw_key(event_id), str(user_id))
    if rank is None:
        return QueueState(admitted=False, sold_out=sold_out, paused=paused)
    if sold_out:
        return QueueState(admitted=False, sold_out=True, paused=paused)

    admitted_count = await _admitted_count(redis, event_id)
    if paused:                                    # admission frozen — hold position
        return QueueState(admitted=False, people_ahead=max(0, rank - admitted_count), paused=True)
    if rank < admitted_count:
        return QueueState(admitted=True)
    return QueueState(admitted=False, people_ahead=rank - admitted_count)


async def admit_deadline(redis: Redis, *, event_id: int, user_id: int) -> float | None:
    """Wall-clock epoch seconds at which this user's rank crosses the admission
    cutoff — or None if they're not registered / admission isn't scheduled yet.

    Both inputs are frozen once the registration window closes (no new
    registrations can change the rank; the rate is config), so the SSE stream
    computes this ONCE and sleeps precisely until it instead of polling for it.

    Derivation from _admitted_count / status():
        admitted  <=>  rank < int(elapsed * RATE)
                  <=>  elapsed >= (rank + 1) / RATE
        =>  admit_at = admit_start + (rank + 1) / RATE
    """
    start = await redis.get(_admit_start_key(event_id))
    if start is None:
        return None
    rank = await redis.zrank(_draw_key(event_id), str(user_id))
    if rank is None:
        return None
    rate = get_settings().QUEUE_ADMISSION_RATE
    return float(start) + (rank + 1) / rate


def _used_key(jti: str) -> str:
    """Single-use marker for one admission token."""
    return f"admission_used:{jti}"


async def refund_admission(redis: Redis, jti: str) -> None:
    """把單次入場券還回去。**只在確定沒有任何訂單意圖成立時**呼叫。

    為什麼需要這個:SETNX 必須發生在扣庫存之前 —— 否則同一張 token 的兩個並發
    請求都會通過檢查,一張入場券換到兩張票。但這樣一來,凡是下單失敗(售完、
    配不出座位、活動不在販售窗、帶錯 zone)都會連帶燒掉入場券,使用者得回去
    重新排隊才能改個張數再試。

    售完是終局的所以以前看不出問題,但「配不出這個張數」本質上是可重試的
    (改成 2 張就成立),而在讀-算-CAS 的配位架構下重試更是常態路徑。所以失敗
    必須把券還回去。

    冪等:DEL 不存在的 key 是 no-op。
    """
    await redis.delete(_used_key(jti))


async def verify_admission(redis: Redis, token: str, *, user_id: int, event_id: int) -> str:
    """Raise AdmissionDenied unless `token` is a valid, in-scope, unused admission pass.

    Checks signature/expiry (jwt), type, that it was issued for this event and this
    user, and that it hasn't been used before (single-use via SETNX on the jti).

    回傳 jti,讓呼叫端能在訂單最終沒有成立時用 `refund_admission` 還回去。
    SETNX 留在這裡而不是搬到下單成功之後,是為了保住單次性的原子性:改成
    「先 EXISTS 檢查、成功後才 SETNX」會開一個窗口讓同一張 token 的兩個並發
    請求都通過。
    """
    settings = get_settings()
    try:
        payload = jwt.decode(token, settings.SECRET_KEY, algorithms=[settings.ALGORITHM])
    except InvalidTokenError:
        raise AdmissionDenied("invalid or expired admission token")
    if payload.get("typ") != "admission":
        raise AdmissionDenied("wrong token type")
    if payload.get("event_id") != event_id:
        raise AdmissionDenied("admission token not valid for this event")
    if payload.get("sub") != str(user_id):
        raise AdmissionDenied("admission token belongs to another user")

    jti = payload.get("jti")
    ttl = max(1, int(payload["exp"] - datetime.now(timezone.utc).timestamp()))
    first_use = await redis.set(_used_key(jti), "1", nx=True, ex=ttl)
    if not first_use:
        raise AdmissionDenied("admission token already used")
    return jti
