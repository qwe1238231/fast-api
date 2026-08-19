"""Stripe webhook 事件的去重(claim)。"""
from datetime import datetime, timedelta

from sqlalchemy import delete
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.stripe_event import StripeEvent


async def claim_event(db: AsyncSession, *, event_id: str, event_type: str) -> bool:
    """試著把這個事件標成「由我處理」。第一次回 True,重送回 False。

    **這個呼叫必須跟後續的狀態變更在同一個交易裡。** 兩個理由:

      - 成功要一起成功:提交之後這個事件就永遠不會再被處理,所以標記與它造成的
        狀態變更必須同進同出。分開提交的話,「標記完了但狀態沒改」會讓那筆付款
        永遠停在錯誤的狀態,而且 Stripe 不會再送。
      - 失敗要一起失敗:處理途中拋例外 → 整個交易回滾 → 標記也不見了 → Stripe
        重送時會被當成新事件正常處理。**這正是我們要的重試語意**,而它是「同一個
        交易」免費附帶的。

    `ON CONFLICT DO NOTHING` 而不是先 SELECT 再 INSERT:後者在並發重送下兩邊都會
    看到「還沒有」而雙雙放行 —— 那正是這張表要防的事。並發時第二個 INSERT 會**等**
    第一個交易結束(唯一索引的鎖),然後才知道自己是重送。
    """
    result = await db.execute(
        insert(StripeEvent)
        .values(event_id=event_id, event_type=event_type)
        .on_conflict_do_nothing(index_elements=["event_id"])
    )
    return result.rowcount == 1


async def purge_events_older_than(db: AsyncSession, *, cutoff: datetime) -> int:
    """刪掉 `cutoff` 之前收到的事件紀錄,回傳刪掉幾筆。

    這張表只增不減的話會隨著付款量無界成長,而它的用途在事件收到幾天後就結束了
    (Stripe 最多重送三天)。保留期比那個窗長很多,是為了事後對帳。
    """
    result = await db.execute(delete(StripeEvent).where(StripeEvent.received_at < cutoff))
    return result.rowcount


def cutoff_for(retention_days: int, *, now: datetime) -> datetime:
    """保留期的邊界。獨立出來讓測試不必操作系統時間。"""
    return now - timedelta(days=retention_days)
