import asyncio
from datetime import datetime, timezone
from typing import Annotated

from fastapi import APIRouter, HTTPException, Query, Request, status
from fastapi.responses import StreamingResponse

from app.api.deps import (
    CurrentAdmin, CurrentUser, DbSession, Redis, StreamUser, client_ip,
    enforce_ip_rate_limit,
)
from app.core.config import get_settings
from app.core.exceptions import EventNotFound
from app.core.security import create_admission_token
from app.crud.event import get_event, list_published_events
from app.db.optimistic import stale_data_as_conflict
from app.services.event_admin import (
    apply_event_update, create_event, update_zone_prices,
)
from app.services.audit import emit_event as emit_audit_event
from app.services.event_cache import invalidate_event_meta
from app.models.event import Event
from app.models.seating import EventZonePrice
from app.schemas.event import (
    EventCreate, EventResponse, EventUpdate, QueueStatusResponse,
)
from app.schemas.seating import (
    ZoneAvailability, ZonePriceResponse, ZonePricesUpdate,
)
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

@router.patch("/{event_id}", response_model=EventResponse)
async def update_event_endpoint(
        event_id: int,
        data: EventUpdate,
        request: Request,
        db: DbSession,
        redis: Redis,
        current_admin: CurrentAdmin,
) -> Event:
    """後台編輯場次。樂觀鎖:body 的 `version` 必須是 GET 回來的那個。

    版本對不上回 409(重新載入、把改動套到新版本上再送一次就會過),不是 5xx。
    兩道關卡都要有 —— apply_event_update 裡的比對擋「表單開太久」,這裡的
    stale_data_as_conflict 擋「兩個管理員前後腳按下儲存」。

    快取失效是 **post-commit** 的獨立步驟,理由跟座位釋放同一個:先清快取再
    commit 的話,一次 rollback 就會讓 Redis 重新載入到「還沒生效的舊值」,而那個
    順序看起來完全正常,不會有任何錯誤。price_cents 與售票窗都在 EventMeta 裡,
    不清的話最多 60 秒內還會按舊價賣。

    稽核同樣是 post-commit:記的是「已經發生的事」。被 409 擋下的那些沒有改變任何
    東西,不進稽核 —— 想知道有沒有人一直撞版本衝突,那是日誌與監控的問題。
    """
    event = await get_event(db, event_id=event_id)
    if event is None:
        raise EventNotFound(event_id=event_id)

    changes = apply_event_update(event, data)
    if not changes:
        return event                       # no-op:沒有欄位變動,不動版本也不清快取

    async with stale_data_as_conflict(
        db, resource="event", resource_id=event_id, expected_version=data.version
    ):
        await db.commit()
    await invalidate_event_meta(redis, event_id=event_id)
    await emit_audit_event(
        redis,
        event_type="event.updated",
        actor_user_id=current_admin.id,
        actor_ip=client_ip(request),
        target_type="event",
        target_id=str(event_id),
        payload={"changes": changes},
    )
    # updated_at 是 server-side onupdate,ORM 不知道資料庫填了什麼,所以 flush 之後
    # 那個屬性是 expired 的。少了這次 refresh,序列化回應時的屬性存取會變成一次
    # lazy load —— 在 async 底下就是 MissingGreenlet,而不是一句「值不對」。
    await db.refresh(event)
    return event


@router.patch("/{event_id}/zone-prices", response_model=list[ZonePriceResponse])
async def update_zone_prices_endpoint(
        event_id: int,
        data: ZonePricesUpdate,
        request: Request,
        db: DbSession,
        redis: Redis,
        current_admin: CurrentAdmin,
) -> list[EventZonePrice]:
    """整批調整分區票價。逐列樂觀鎖:每一列各帶自己的 version。

    整批進同一個 transaction —— 任何一列版本不符就全部退回,管理員不會拿到一份
    改到一半的價格表。回傳的是**完整**的價格表(含沒改動的那幾列),因為下一次
    儲存需要每一列的新版本。

    只清 EventMeta 快取,不清選區畫面那份:前者 60 秒 TTL 而且是**下單算錢**要
    讀的,慢一秒都是按錯的價格賣票;後者 2 秒 TTL、純顯示,讓它自然過期就好。
    """
    event = await get_event(db, event_id=event_id)
    if event is None:
        raise EventNotFound(event_id=event_id)

    prices, changes = await update_zone_prices(db, event, data)
    if not changes:
        return prices

    # resource 用複數、id 用 event_id:flush 撞上時只知道「這批裡有一列輸了」,
    # 答不出是哪一列(rowcount 對不上而已)。逐列的那道才報得出 zone_id。
    async with stale_data_as_conflict(
        db, resource="event_zone_prices", resource_id=event_id
    ):
        await db.commit()
    await invalidate_event_meta(redis, event_id=event_id)
    # 改價是直接影響營收的操作,而 updated_at 只說得出「有人改過」。稽核的
    # payload 是 {zone_id: {from, to}} —— 事後查帳要的是金額本身,不是時間戳。
    await emit_audit_event(
        redis,
        event_type="event.zone_prices_updated",
        actor_user_id=current_admin.id,
        actor_ip=client_ip(request),
        target_type="event",
        target_id=str(event_id),
        payload={"changes": changes},
    )
    return prices


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
    request: Request,
    db: DbSession,
    redis: Redis,
) -> list[ZoneAvailability]:
    """選區畫面:每一區的票價、剩餘席數,以及**現在配得出來的張數**。

    回可行張數集合而不是「最大連號長度」,是為了把約束編碼進前端的可選集合 ——
    使用者就不會送出一個註定失敗的請求。剩下的拒絕只有 race(「這個位置剛被買走」),
    那種使用者能理解;「你不能買這裡因為會留下一個空位」則不能。

    唯讀,不進 Lua:瀏覽量遠大於下單量,快照過時是可接受的。結果有 2 秒快取 ——
    開賣前後這裡會被瘋狂刷新,而每次真實計算是「每個 zone 讀 runs+geom + 跑
    feasible_quantities」。無認證的端點更需要限流。
    """
    await enforce_ip_rate_limit(
        request, redis,
        bucket=f"zones:{event_id}",
        rate=f"{get_settings().ZONES_LIST_LIMIT_PER_MINUTE}/minute",
    )
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