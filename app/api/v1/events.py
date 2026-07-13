from datetime import datetime, timezone
from typing import Annotated

from fastapi import APIRouter, HTTPException, Query, status

from app.api.deps import CurrentAdmin, CurrentUser, DbSession, Redis
from app.core.config import get_settings
from app.core.exceptions import EventNotFound
from app.core.security import create_admission_token
from app.crud.event import create_event, get_event, list_published_events
from app.models.event import Event
from app.schemas.event import EventCreate, EventResponse, QueueStatusResponse
from app.services.publish_event import publish_event
from app.services.waiting_room import window, register as queue_register, status as queue_status, QueueState
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