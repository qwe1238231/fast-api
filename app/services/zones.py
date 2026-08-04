"""選區資訊的組裝 —— 唯讀路徑。

刻意跟 seat_runs 分開:那邊是下單熱路徑(要進 Lua、要 CAS),這邊是瀏覽路徑
(純讀、可過時、絕不佔用 Redis 的原子區)。兩者目標相反,不要共用同一套機制。
"""
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from redis.asyncio import Redis

from app.core.exceptions import EventNotFound, SeatsNotAssigned
from app.models.event import Event
from app.models.seating import SeatBlock, SeatHold, Zone
from app.schemas.seating import SeatedOrderDetail, ZoneAvailability
from app.services.pricing import load_zone_prices
from app.services.seat_runs import (
    MAX_TICKETS_PER_ORDER,
    is_relaxed,
    read_zone_state,
    seat_labels,
)
from app.services.seating import ENDGAME_POLICY, NORMAL_POLICY, feasible_quantities


async def list_zone_availability(
    db: AsyncSession, redis: Redis, *, event_id: int
) -> list[ZoneAvailability]:
    """每一區的票價、剩餘席數與可行張數。無座位圖的場次回空清單。"""
    venue_id = await db.scalar(select(Event.venue_id).where(Event.id == event_id))
    if venue_id is None:
        # 場次不存在,或它沒有座位圖(舊的純計數器路徑)。前者要明確報錯,
        # 後者回空清單 —— 沒有區可選就是正確答案。
        if not await db.scalar(select(Event.id).where(Event.id == event_id)):
            raise EventNotFound(event_id=event_id)
        return []

    zones = (
        await db.scalars(
            select(Zone).where(Zone.venue_id == venue_id).order_by(Zone.display_order)
        )
    ).all()
    prices = await load_zone_prices(db, event_id=event_id, venue_id=venue_id)

    out: list[ZoneAvailability] = []
    for zone in zones:
        price = prices.get(zone.id)
        if price is None:
            continue        # 這場沒設這一區的票價 → 不可賣,就別出現在選單上
        state = await read_zone_state(redis, event_id=event_id, zone_id=zone.id)
        # 可行張數必須用**這個 zone 當下實際生效的策略**算,否則收尾期放寬之後
        # 前端會繼續 disable 掉其實已經買得到的張數。
        policy = (
            ENDGAME_POLICY
            if await is_relaxed(redis, event_id=event_id, zone_id=zone.id)
            else NORMAL_POLICY
        )
        out.append(
            ZoneAvailability(
                zone_id=zone.id,
                name=zone.name,
                display_order=zone.display_order,
                price_cents=price,
                available=state.remaining,
                available_quantities=feasible_quantities(
                    state.runs, state.geometry, MAX_TICKETS_PER_ORDER, policy
                ),
            )
        )
    return out


async def describe_order_seats(db: AsyncSession, order) -> SeatedOrderDetail:
    """把一筆已確認訂單的 hold 區間翻成人看的座號。

    座號不存在 hold 上,而是用 `pos` 去 join `seats` 推導 —— pos 是稠密索引(連續性
    只看它),label 是門牌(會跳過 4、13,或單雙號分邊)。兩者分開是整個設計的前提。
    """
    row = (
        await db.execute(
            select(Zone.name, SeatBlock.row_label, SeatHold.block_id,
                   SeatHold.start_pos, SeatHold.length)
            .join(SeatBlock, SeatBlock.id == SeatHold.block_id)
            .join(Zone, Zone.id == SeatBlock.zone_id)
            .where(SeatHold.order_id == order.id)
        )
    ).one_or_none()
    if row is None:
        raise SeatsNotAssigned(order_id=order.id)
    zone_name, row_label, block_id, start_pos, length = row
    return SeatedOrderDetail(
        zone_name=zone_name,
        row_label=row_label,
        labels=await seat_labels(
            db, block_id=block_id, start_pos=start_pos, length=length
        ),
    )
