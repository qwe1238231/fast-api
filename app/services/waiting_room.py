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
from datetime import datetime, timedelta, timezone

from redis.asyncio import Redis

from app.core.config import get_settings
from app.models.event import Event


def _draw_key(event_id: int) -> str:
    return f"queue:{event_id}:draw"


def _admit_start_key(event_id: int) -> str:
    return f"queue:{event_id}:admit_start"   # epoch seconds when admission begins (window close)


def _salt_key(event_id: int) -> str:
    return f"queue:{event_id}:salt"


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


async def status(redis: Redis, *, event_id: int, user_id: int) -> tuple[bool, int | None]:
    """Return (admitted, people_ahead).

    people_ahead is None if not registered, else the number of not-yet-admitted
    users ahead in the draw (0 == you're next). None too once admitted.
    """
    rank = await redis.zrank(_draw_key(event_id), str(user_id))
    if rank is None:
        return False, None
    admitted = await _admitted_count(redis, event_id)
    if rank < admitted:
        return True, None
    return False, rank - admitted
