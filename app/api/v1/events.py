import asyncio
from datetime import datetime, timezone
from typing import Annotated

from fastapi import APIRouter, HTTPException, Query, Request, status
from fastapi.responses import StreamingResponse

from app.api.deps import CurrentAdmin, CurrentUser, DbSession, Redis, StreamUser
from app.core.config import get_settings
from app.core.exceptions import EventNotFound
from app.core.security import create_admission_token
from app.crud.event import create_event, get_event, list_published_events
from app.models.event import Event
from app.schemas.event import EventCreate, EventResponse, QueueStatusResponse
from app.schemas.seating import ZoneAvailability
from app.services.zones import list_zone_availability
from app.services.publish_event import publish_event
from app.services.queue_events import register as sse_register, unregister as sse_unregister
from app.services.waiting_room import (
    window, register as queue_register, status as queue_status, QueueState, admit_deadline,
)
from app.services.rate_limit import enforce_rate_limit


router = APIRouter(prefix="/events", tags=["events"])


@router.post("/", response_model=EventResponse, status_code=status.HTTP_201_CREATED)
async def create_event_endpoint(
        data: EventCreate,
        db: DbSession,
        current_admin: CurrentAdmin,
) -> Event:
    event = await create_event(db, data)
    await db.commit()
    return event

@router.post("/{event_id}/publish", response_model=EventResponse)
async def publish_event_endpoint(
        event_id: int,
        db: DbSession,
        redis: Redis,
        current_admin: CurrentAdmin,
) -> Event:
    event = await get_event(db, event_id=event_id)
    if event is None:
        raise EventNotFound(event_id=event_id)
    await publish_event(db, redis, event)
    await db.commit()
    await db.refresh(event)
    return event

@router.get("/", response_model=list[EventResponse])
async def list_published_endpoint(
        db: DbSession,
        offset: Annotated[int, Query(ge=0)] = 0,
        limit: Annotated[int, Query(ge=1, le=100)] = 50,
) -> list[Event]:
    return await list_published_events(db, offset=offset, limit=limit)

@router.get("/{event_id}/zones", response_model=list[ZoneAvailability])
async def list_zones_endpoint(
    event_id: int,
    db: DbSession,
    redis: Redis,
) -> list[ZoneAvailability]:
    """選區畫面:每一區的票價、剩餘席數,以及**現在配得出來的張數**。

    回可行張數集合而不是「最大連號長度」,是為了把約束編碼進前端的可選集合 ——
    使用者就不會送出一個註定失敗的請求。剩下的拒絕只有 race(「這個位置剛被買走」),
    那種使用者能理解;「你不能買這裡因為會留下一個空位」則不能。

    唯讀,不進 Lua:瀏覽量遠大於下單量,快照過時是可接受的。
    """
    return await list_zone_availability(db, redis, event_id=event_id)


@router.get("/{event_id}", response_model=EventResponse)
async def get_event_endpoint(event_id: int, db: DbSession) -> Event:
    event = await get_event(db, event_id=event_id)
    if event is None:
        raise EventNotFound(event_id=event_id)
    return event


def _queue_response(state: QueueState, *, user_id: int, event_id: int) -> QueueStatusResponse:
    token = None
    if state.admitted:
        token = create_admission_token(
            user_id=user_id,
            event_id=event_id,
            ttl_seconds=get_settings().QUEUE_ADMISSION_TOKEN_TTL_SECONDS,
        )
    done = state.admitted or state.sold_out    # stop polling once admitted or nothing left to buy
    return QueueStatusResponse(
        admitted=state.admitted,
        sold_out=state.sold_out,
        paused=state.paused,
        people_ahead=state.people_ahead,
        poll_after_seconds=None if done else 5,
        access_token=token,
    )


@router.post("/{event_id}/queue", response_model=QueueStatusResponse)
async def join_queue(
        event_id: int,
        current_user: CurrentUser,
        db: DbSession,
        redis: Redis,
) -> QueueStatusResponse:
    """Enter the waiting-room lottery for this event (idempotent, non-re-rollable)."""
    await enforce_rate_limit(
        redis,
        f"qjoin:{event_id}:{current_user.id}",
        limit=get_settings().QUEUE_JOIN_LIMIT_PER_MINUTE,
        window_seconds=60,
    )
    event = await get_event(db, event_id=event_id)
    if event is None:
        raise EventNotFound(event_id=event_id)
    now = datetime.now(timezone.utc)
    opens, closes = window(event)
    if now < opens:
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail="queue not open yet")
    if now >= closes:
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail="queue registration closed")
    await queue_register(redis, event=event, user_id=current_user.id)
    state = await queue_status(redis, event_id=event_id, user_id=current_user.id)
    return _queue_response(state, user_id=current_user.id, event_id=event_id)


@router.get("/{event_id}/queue/status", response_model=QueueStatusResponse)
async def queue_status_endpoint(
        event_id: int,
        current_user: CurrentUser,
        redis: Redis,
) -> QueueStatusResponse:
    """Poll your waiting-room position / admission. Redis-only (no DB on the hot poll path)."""
    state = await queue_status(redis, event_id=event_id, user_id=current_user.id)
    return _queue_response(state, user_id=current_user.id, event_id=event_id)


_SSE_HEARTBEAT_SECONDS = 20   # keep-alive + poll fallback if a poke is missed (< ALB's 60s idle)
_SSE_MAX_SECONDS = 300        # cap one connection; the browser's EventSource then auto-reconnects


@router.get("/{event_id}/queue/stream")
async def queue_stream(
        event_id: int,
        current_user: StreamUser,
        redis: Redis,
        request: Request,
) -> StreamingResponse:
    """Server-Sent Events stream of waiting-room position/admission — the push
    alternative to polling /queue/status.

    Correctness lives in waiting_room.status (the single source of truth); the
    stream re-reads it on every wake. Immediacy is layered on top:
      - admission is a pure function of time, so we compute the exact deadline
        and sleep until it (no polling for it);
      - pause / sold-out are real events, delivered as a Pub/Sub "poke" via the
        per-process subscriber (app/services/queue_events.py) which nudges this
        connection's mailbox.
    A wake from any source — poke, admit deadline, or heartbeat timeout — just
    re-reads status(). Each frame also doubles as the ALB keep-alive.

    AUTH: via get_stream_user — the JWT comes from the Authorization header (API
    clients) or an ?access_token= query param (browsers, since native EventSource
    can't send headers). See get_stream_user for the token-in-URL security note.
    """
    def _frame(state: QueueState) -> str:
        body = _queue_response(state, user_id=current_user.id, event_id=event_id).model_dump_json()
        return f"data: {body}\n\n"

    async def gen():
        loop = asyncio.get_running_loop()
        started = loop.time()

        # Register the mailbox BEFORE the first read: a poke fired in between then
        # lands in the maxsize=1 mailbox instead of being lost, so the loop's first
        # wait returns at once. finally covers every exit, incl. the early return.
        mailbox = sse_register(event_id)
        try:
            # First frame immediately: the authoritative state right now. If we're
            # already admitted / sold out we never enter the wait at all.
            state = await queue_status(redis, event_id=event_id, user_id=current_user.id)
            yield _frame(state)
            if state.admitted or state.sold_out:
                return

            while loop.time() - started < _SSE_MAX_SECONDS:
                # Recompute each wake: a rank only freezes at window close, so an
                # early joiner's deadline self-corrects once it's real. Tighten the
                # wait only while the deadline is still ahead; once it has passed,
                # fall back to the heartbeat and rely on a poke (e.g. pause lifted)
                # — never spin at a sub-second floor (that would hammer Redis right
                # when admission is paused, i.e. when downstream is already sick).
                timeout = float(_SSE_HEARTBEAT_SECONDS)
                admit_at = await admit_deadline(redis, event_id=event_id, user_id=current_user.id)
                if admit_at is not None:
                    until_admit = admit_at - datetime.now(timezone.utc).timestamp()
                    if until_admit > 0:
                        timeout = min(timeout, until_admit)

                try:
                    await asyncio.wait_for(mailbox.get(), timeout)
                except asyncio.TimeoutError:
                    pass                     # woke by heartbeat / admit deadline, not a poke

                if await request.is_disconnected():
                    return
                state = await queue_status(redis, event_id=event_id, user_id=current_user.id)
                yield _frame(state)
                if state.admitted or state.sold_out:
                    return                   # terminal — close the stream
        finally:
            sse_unregister(event_id, mailbox)

    return StreamingResponse(
        gen(),
        media_type="text/event-stream",
        headers={
            "Cache-Control": "no-cache",
            "X-Accel-Buffering": "no",       # ask proxies not to buffer the stream
        },
    )