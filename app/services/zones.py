"""選區資訊的組裝(唯讀熱路徑)+ 後台編輯 zone 本身(低頻寫入)。

刻意跟 seat_runs 分開:那邊是下單熱路徑(要進 Lua、要 CAS),這邊是瀏覽路徑
(純讀、可過時、絕不佔用 Redis 的原子區)。兩者目標相反,不要共用同一套機制。

檔案後半的後台編輯是第三種東西:低頻、低競爭、但不能靜默覆蓋 —— 所以用樂觀鎖,
不是 CAS 也不是 Lua。
"""
import json
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from redis.asyncio import Redis

from app.core.exceptions import EventNotFound, SeatsNotAssigned, ZoneNameTaken
from app.core.config import max_purchasable
from app.db.optimistic import require_version
from app.models.event import Event
from app.models.seating import SeatBlock, SeatHold, Zone
from app.schemas.seating import SeatedOrderDetail, ZoneAvailability, ZoneUpdate
from app.services.audit import FieldDiff
from app.services.pricing import load_zone_prices
from app.services.seat_runs import (
    read_zone_snapshots,
    seat_labels,
)
from app.services.seating import ENDGAME_POLICY, NORMAL_POLICY, feasible_quantities


#: 選區畫面的快取秒數。刻意很短:剩餘席數每筆訂單都在變,但開賣前後這個端點會被
#: 瘋狂刷新,2 秒的合併就能把 thundering herd 壓成每 2 秒一次真實計算。使用者感覺
#: 不到 2 秒的延遲,而「快照過時可接受」本來就是這條唯讀路徑的前提。
_ZONES_CACHE_SECONDS = 2


def _zones_cache_key(event_id: int) -> str:
    return f"event:{event_id}:zones:cache"


async def list_zone_availability(
    db: AsyncSession, redis: Redis, *, event_id: int
) -> list[ZoneAvailability]:
    """每一區的票價、剩餘席數與可行張數。無座位圖的場次回空清單。"""
    cached = await redis.get(_zones_cache_key(event_id))
    if cached is not None:
        return [ZoneAvailability(**row) for row in json.loads(cached)]
    fresh = await _compute_zone_availability(db, redis, event_id=event_id)
    await redis.set(
        _zones_cache_key(event_id),
        json.dumps([row.model_dump() for row in fresh]),
        ex=_ZONES_CACHE_SECONDS,
    )
    return fresh


async def _compute_zone_availability(
    db: AsyncSession, redis: Redis, *, event_id: int
) -> list[ZoneAvailability]:
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

    # 只有定價的 zone 才可賣,所以先篩再讀 —— 沒必要為不會列出的 zone 打 Redis。
    sellable = [zone for zone in zones if zone.id in prices]
    snapshots = await read_zone_snapshots(
        redis, event_id=event_id, zone_ids=[zone.id for zone in sellable]
    )

    out: list[ZoneAvailability] = []
    for zone in sellable:
        snapshot = snapshots[zone.id]
        # 可行張數必須用**這個 zone 當下實際生效的策略**算,否則收尾期放寬之後
        # 前端會繼續 disable 掉其實已經買得到的張數。
        policy = ENDGAME_POLICY if snapshot.relaxed else NORMAL_POLICY
        out.append(
            ZoneAvailability(
                zone_id=zone.id,
                name=zone.name,
                display_order=zone.display_order,
                price_cents=prices[zone.id],
                available=snapshot.state.remaining,
                available_quantities=feasible_quantities(
                    snapshot.state.runs,
                    snapshot.state.geometry,
                    max_purchasable(),
                    policy,
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
            select(Zone.name, SeatBlock.row_label, SeatBlock.block_index,
                   SeatHold.block_id, SeatHold.start_pos, SeatHold.length)
            .join(SeatBlock, SeatBlock.id == SeatHold.block_id)
            .join(Zone, Zone.id == SeatBlock.zone_id)
            .where(SeatHold.order_id == order.id)
        )
    ).one_or_none()
    if row is None:
        raise SeatsNotAssigned(order_id=order.id)
    zone_name, row_label, block_index, block_id, start_pos, length = row
    return SeatedOrderDetail(
        zone_name=zone_name,
        row_label=row_label,
        block_index=block_index,
        labels=await seat_labels(
            db, block_id=block_id, start_pos=start_pos, length=length
        ),
    )


# ── 後台編輯 ────────────────────────────────────────────────────────────────
# 唯讀路徑到這裡為止。以下是管理員改「票種名稱 / 顯示順序」的寫入路徑。

def apply_zone_update(zone: Zone, data: ZoneUpdate) -> FieldDiff:
    """把部分更新套到 zone 上,回傳實際變動的 before/after(空 dict = 沒變)。

    先比對版本再動欄位(同 apply_event_update)。名稱撞號**不在這裡驗** ——
    見 unique_zone_name_guard。
    """
    require_version(zone, expected=data.version, resource="zone", resource_id=zone.id)

    changes = data.model_dump(exclude_unset=True, exclude={"version"})
    diff: FieldDiff = {}
    for field, value in changes.items():
        before = getattr(zone, field)
        if before != value:
            setattr(zone, field, value)
            diff[field] = {"from": before, "to": value}
    return diff


@asynccontextmanager
async def unique_zone_name_guard(db: AsyncSession, zone: Zone) -> AsyncIterator[None]:
    """把 uq_zones_venue_name 的 IntegrityError 翻成 ZoneNameTaken(409)。

    刻意不先 SELECT 檢查名稱有沒有被佔用:那是 TOCTOU —— 兩個管理員同時改成同一個
    名字,兩邊的預檢都會過,然後其中一個照樣撞上唯一索引、照樣 500。唯一索引才是
    權威,所以直接攔它丟出來的例外。順帶少一次查詢。

    比對約束名而不是把所有 IntegrityError 都當成撞名:外鍵、check constraint 也走
    同一個例外,一律回 409「名稱已存在」會把真正的 bug 講成使用者的錯。
    """
    # 先抓下來:rollback 會 expire session 裡的所有物件,之後再讀 zone.name 就是
    # 一次 lazy load(async 底下等於 MissingGreenlet),而且讀到的會是資料庫裡的
    # 舊名字,不是這次想改成的那個。
    venue_id, attempted_name = zone.venue_id, zone.name
    try:
        yield
    except IntegrityError as exc:
        await db.rollback()
        if "uq_zones_venue_name" in str(exc.orig):
            raise ZoneNameTaken(venue_id=venue_id, name=attempted_name) from exc
        raise
