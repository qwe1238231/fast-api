from fastapi import APIRouter, status

from app.api.deps import CurrentAdmin, DbSession, Redis
from app.core.exceptions import EventNotFound
from app.crud.event import create_event, get_event, list_published_events
from app.models.event import Event
from app.schemas.event import EventCreate, EventResponse
from app.services.publish_event import publish_event
from app.services.inventory import reconcile_inventory

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
async def list_published_endpoint(db: DbSession) -> list[Event]:
    return await list_published_events(db)

@router.get("/{event_id}", response_model=EventResponse)
async def get_event_endpoint(event_id: int, db: DbSession) -> Event:
    event = await get_event(db, event_id=event_id)
    if event is None:
        raise EventNotFound(event_id=event_id)
    return event

@router.post("/{event_id}/reconcile-inventory")
async def reconcile_inventory_endpoint(
    event_id: int,
    db: DbSession,
    redis: Redis,
    current_admin: CurrentAdmin,
) -> dict:
    available = await reconcile_inventory(db, redis, event_id=event_id)
    return{"event_id": event_id, "available": available}