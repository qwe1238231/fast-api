"""分區票價 —— 訂單金額的單一來源。

所有算錢的地方都必須走這裡。理由不是整潔,是**分區票價與單一票價會長期共存**:
座位圖的 migration 是純加法,`events.venue_id` 為 NULL 的舊場次照舊走單一票價。
把這個分支收進一個函式,呼叫端就不必知道差別,而之後要改價格模型也只改這裡。

熱路徑不查 DB。價格表隨 EventMeta 一起快取(見 event_cache),所以 `unit_price`
是純函式、零 I/O。這也讓「zone 屬於這個場次嗎」變成一次 dict 查找 ——
price map 是用 `event_zone_prices JOIN zones WHERE zones.venue_id = event.venue_id`
建的,所以 **key 存在本身就同時證明了「屬於本場館」與「已設定票價」**。
"""
from collections.abc import Mapping
from typing import Protocol

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.exceptions import ZoneNotForEvent, ZoneRequired
from app.models.seating import EventZonePrice, Zone


class PricingContext(Protocol):
    """算錢需要的欄位。用 Protocol 而不是 import EventMeta,免得 pricing 反過來
    依賴 cache 層 —— 測試也就能用一個十行的 stub 而不必準備 Redis。"""

    event_id: int
    venue_id: int | None
    zone_prices: Mapping[int, int]
    price_cents: int


async def load_zone_prices(
    db: AsyncSession, *, event_id: int, venue_id: int | None
) -> dict[int, int]:
    """讀出這個場次所有「可賣」的分區票價:zone_id → 單價(分)。

    `venue_id is None`(無座位圖的場次)直接回空 dict,連查詢都不發 —— 舊路徑
    不該為新功能付任何代價。

    JOIN zones 過濾 venue 不只是為了正確性,也是為了讓回傳的 dict 可以被當成
    白名單使用:key 在裡面 == 這個 zone 可以賣給這個場次。
    """
    if venue_id is None:
        return {}
    rows = await db.execute(
        select(EventZonePrice.zone_id, EventZonePrice.price_cents)
        .join(Zone, Zone.id == EventZonePrice.zone_id)
        .where(EventZonePrice.event_id == event_id, Zone.venue_id == venue_id)
    )
    return {zone_id: price for zone_id, price in rows}


def unit_price(
    *,
    event_id: int,
    venue_id: int | None,
    zone_prices: Mapping[int, int],
    fallback_price_cents: int,
    zone_id: int | None,
) -> int:
    """一張票的單價。純函式 —— 參數全部來自已快取的 EventMeta。

    座位場次缺某一區的票價時**不 fallback**,直接拒絕。fallback 只服務
    `venue_id is None` 的舊路徑;拿它去補座位場次的設定漏洞,結果會是把搖滾區
    按最低價賣掉,而且沒有任何錯誤 —— 那種營收 bug 要等對帳才會發現。
    """
    if venue_id is None:
        if zone_id is not None:
            raise ZoneNotForEvent(event_id, zone_id, reason="event has no seat map")
        return fallback_price_cents

    if zone_id is None:
        raise ZoneRequired(event_id)
    price = zone_prices.get(zone_id)
    if price is None:
        # 不存在 / 屬於別場館 / 這場沒設價 —— 對外不區分,避免洩漏場館結構。
        raise ZoneNotForEvent(event_id, zone_id, reason="unknown zone or unpriced")
    return price


def order_total(
    *,
    event_id: int,
    venue_id: int | None,
    zone_prices: Mapping[int, int],
    fallback_price_cents: int,
    zone_id: int | None,
    quantity: int,
) -> int:
    """訂單總額。集中乘法,避免有人拿錯的單價自己乘。"""
    if quantity < 1:
        raise ValueError("quantity 至少為 1")
    return quantity * unit_price(
        event_id=event_id,
        venue_id=venue_id,
        zone_prices=zone_prices,
        fallback_price_cents=fallback_price_cents,
        zone_id=zone_id,
    )


def price_for(meta: PricingContext, *, zone_id: int | None) -> int:
    """從已載入的 meta 取單價 —— 呼叫端的常用入口。"""
    return unit_price(
        event_id=meta.event_id,
        venue_id=meta.venue_id,
        zone_prices=meta.zone_prices,
        fallback_price_cents=meta.price_cents,
        zone_id=zone_id,
    )


def total_for(meta: PricingContext, *, zone_id: int | None, quantity: int) -> int:
    """從已載入的 meta 算訂單總額 —— 呼叫端的常用入口。"""
    return order_total(
        event_id=meta.event_id,
        venue_id=meta.venue_id,
        zone_prices=meta.zone_prices,
        fallback_price_cents=meta.price_cents,
        zone_id=zone_id,
        quantity=quantity,
    )
