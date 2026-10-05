"""Zone 的後台編輯。

掛在 `/zones/{id}` 而不是 `/venues/{vid}/zones/{id}`:zone id 全域唯一,多一層
路徑只是要求呼叫端多帶一個它已經知道的值,而伺服器還得多驗一次兩者相符 ——
沒有換到任何東西。

**Zone 是 venue-scoped 的。** 改名會連動該場館的**所有**場次,包含已經結束的
——「A 區」改成「搖滾區」之後,三年前那張票的頁面也會跟著變。訂單只快照了金額
(total_price_cents),沒有快照區名。要讓歷史訂單不受影響,得在 orders 或
seat_holds 上多存一份區名快照,那是另一件事,不是鎖能解決的。
"""
from fastapi import APIRouter, Request

from app.api.deps import CurrentAdmin, DbSession, Redis, client_ip
from app.crud.zone import get_zone
from app.core.exceptions import ZoneNotFound
from app.db.optimistic import stale_data_as_conflict
from app.models.seating import Zone
from app.schemas.seating import ZoneResponse, ZoneUpdate
from app.services.audit import emit_event as emit_audit_event
from app.services.zones import apply_zone_update, unique_zone_name_guard


router = APIRouter(prefix="/zones", tags=["zones"])


@router.get("/{zone_id}", response_model=ZoneResponse)
async def get_zone_endpoint(
        zone_id: int,
        db: DbSession,
        current_admin: CurrentAdmin,
) -> Zone:
    """後台編輯頁的讀取。回應帶著 version —— 沒有它,PATCH 就無從帶回。"""
    zone = await get_zone(db, zone_id=zone_id)
    if zone is None:
        raise ZoneNotFound(zone_id=zone_id)
    return zone


@router.patch("/{zone_id}", response_model=ZoneResponse)
async def update_zone_endpoint(
        zone_id: int,
        data: ZoneUpdate,
        request: Request,
        db: DbSession,
        redis: Redis,
        current_admin: CurrentAdmin,
) -> Zone:
    """改票種名稱 / 顯示順序。樂觀鎖:body 的 `version` 必須是 GET 回來的那個。

    不清任何快取:選區畫面那份只有 2 秒 TTL(純顯示),而 EventMeta 快取裡沒有
    區名 —— 它只放下單算錢要用的東西。

    改名會連動該場館的所有場次(見模組 docstring),而訂單沒有快照區名 ——
    所以「誰在什麼時候把 A 改成 B」只存在於稽核紀錄裡。這是這條 emit 唯一的
    歷史來源,不是錦上添花。
    """
    zone = await get_zone(db, zone_id=zone_id)
    if zone is None:
        raise ZoneNotFound(zone_id=zone_id)

    changes = apply_zone_update(zone, data)
    if not changes:
        return zone

    # 兩層順序有意義:內層先攔撞名(IntegrityError),外層攔版本(StaleDataError)。
    # 兩者互斥,誰在外面其實都對,但由內而外「先具體後一般」比較好讀。
    async with stale_data_as_conflict(
        db, resource="zone", resource_id=zone_id, expected_version=data.version
    ), unique_zone_name_guard(db, zone):
        await db.commit()
    await emit_audit_event(
        redis,
        event_type="zone.updated",
        actor_user_id=current_admin.id,
        actor_ip=client_ip(request),
        target_type="zone",
        target_id=str(zone_id),
        payload={"venue_id": zone.venue_id, "changes": changes},
    )
    return zone
