"""Live waiting-room notifications — a per-process fan-out over one Redis
Pub/Sub subscription.

Why this exists: the SSE waiting-room stream (app/api/v1/events.py) wants to
react *immediately* to admission-affecting events (circuit-breaker pause/resume,
and — later — sold-out/restock) instead of only noticing them on its heartbeat
re-check.

Two levels of fan-out keep Redis connections bounded (Redis `maxclients`):

    publisher --PUBLISH--> Redis --(1) one message per process--> the single
    subscriber in each process --(2) in-memory--> every local SSE mailbox

So there is exactly ONE Redis subscriber connection per process, no matter how
many SSE connections it serves — "Redis connection count" is decoupled from
"SSE connection count".

The message is only a "poke": receivers re-read the authoritative state
(waiting_room.status) rather than trusting the payload. A lost or duplicated
message is therefore harmless (Pub/Sub is fire-and-forget) — Pub/Sub makes the
update *fast*, waiting_room.status keeps it *correct*.
"""
import asyncio
import contextlib
import logging
import random
from collections import defaultdict

from redis.asyncio import Redis

logger = logging.getLogger(__name__)

_CHANNEL_PATTERN = "queue:*:events"        # one psubscribe covers every event's channel
_GLOBAL_CHANNEL = "queue:all:events"       # pause/resume is global, not per-event


def _channel(event_id: int) -> str:
    return f"queue:{event_id}:events"


def _event_id_from_channel(channel: str) -> int | None:
    """Parse "queue:{id}:events" -> id; None for anything else (e.g. the global channel)."""
    parts = channel.split(":")
    if len(parts) == 3 and parts[0] == "queue" and parts[2] == "events":
        try:
            return int(parts[1])
        except ValueError:
            return None
    return None


# Process-global registry: event_id -> mailboxes of the SSE connections waiting on it.
_mailboxes: dict[int, set[asyncio.Queue]] = defaultdict(set)


def register(event_id: int) -> asyncio.Queue:
    """Create and register a mailbox for one SSE connection. Call unregister when done."""
    mailbox: asyncio.Queue = asyncio.Queue(maxsize=1)
    _mailboxes[event_id].add(mailbox)
    return mailbox


def unregister(event_id: int, mailbox: asyncio.Queue) -> None:
    """Remove a connection's mailbox and drop the event's bucket once empty."""
    bucket = _mailboxes.get(event_id)
    if bucket is None:
        return
    bucket.discard(mailbox)
    if not bucket:
        del _mailboxes[event_id]


def _nudge(mailbox: asyncio.Queue) -> None:
    try:
        mailbox.put_nowait(None)
    except asyncio.QueueFull:
        pass   # a poke is already pending -> coalesce (one re-read catches up to latest)


def _wake(event_id: int) -> None:
    """Nudge every local connection waiting on `event_id`. Never blocks on a slow one."""
    for mailbox in _mailboxes.get(event_id, ()):
        _nudge(mailbox)


def _wake_all() -> None:
    """Nudge every local connection (for the global pause/resume poke)."""
    for bucket in _mailboxes.values():
        for mailbox in bucket:
            _nudge(mailbox)


async def publish_event_poke(redis: Redis, event_id: int) -> None:
    """Broadcast 'something changed for this event' (e.g. sold-out / restock).

    Best-effort: a poke only tells receivers to re-read waiting_room.status (the
    source of truth), so a failed publish is harmless. It must NEVER propagate out
    of an authoritative path (a committed reserve/release) and fail that request.
    """
    try:
        await redis.publish(_channel(event_id), "1")
    except Exception:
        logger.warning("event poke failed for event %s (harmless; status recovers)", event_id, exc_info=True)


async def publish_global_poke(redis: Redis) -> None:
    """Broadcast a change that affects every event (admission pause / resume).

    Best-effort, for the same reason as publish_event_poke.
    """
    try:
        await redis.publish(_GLOBAL_CHANNEL, "1")
    except Exception:
        logger.warning("global poke failed (harmless; status recovers)", exc_info=True)


async def run_subscriber(redis: Redis) -> None:
    """Process-wide background task: one pattern subscription, fan out in memory.

    Resilient by design: on any Redis error it logs, closes, and retries, so a
    Redis blip only degrades streams to heartbeat-only re-checks (correctness is
    never at risk — that is waiting_room.status's job). Started in the app
    lifespan; cancelled on shutdown.
    """
    failures = 0
    while True:
        pubsub = redis.pubsub()
        try:
            await pubsub.psubscribe(_CHANNEL_PATTERN)
            failures = 0                              # (re)subscribed cleanly -> reset backoff
            async for message in pubsub.listen():
                if message.get("type") != "pmessage":
                    continue                          # skip the psubscribe confirmation
                channel = message["channel"]
                if channel == _GLOBAL_CHANNEL:
                    _wake_all()
                    continue
                event_id = _event_id_from_channel(channel)
                if event_id is not None:
                    _wake(event_id)
        except asyncio.CancelledError:
            with contextlib.suppress(Exception):
                await pubsub.aclose()
            raise                                     # shutdown: propagate cancellation
        except Exception:                             # noqa: BLE001 — stay alive on any Redis error
            # Full traceback once, then throttle to a single line so a sustained
            # outage can't flood the logs; capped exponential backoff + jitter
            # avoids a tight reconnect loop.
            if failures == 0:
                logger.exception("queue event subscriber failed; retrying with backoff")
            else:
                logger.warning("queue event subscriber still down (attempt %d)", failures + 1)
            with contextlib.suppress(Exception):
                await pubsub.aclose()
            failures += 1
            await asyncio.sleep(min(30.0, 2 ** min(failures, 5)) + random.uniform(0, 1))
