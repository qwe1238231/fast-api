from datetime import datetime

from sqlalchemy import DateTime, Index, String
from sqlalchemy.orm import Mapped, mapped_column
from sqlalchemy.sql import func

from app.db.base import Base


class StripeEvent(Base):
    """已經處理過的 Stripe webhook 事件。**這張表是一把鎖,不只是一份紀錄。**

    Stripe 對同一個事件會重送(逾時、網路抖動,或它自己的 at-least-once 保證),而且
    可以**並發**重送。少了這張表,兩個同時抵達的 `payment_intent.succeeded` 會這樣走:

        A: 讀到 order 是 PENDING → CAS 成 PAID → 成交
        B: 讀到 order 是 PENDING → CAS **失敗**(A 先到)→ 走「訂單中途離開 PENDING」
           那條分支 → **退款**

    結果是同一個人拿到票、又拿到退款。CAS 保護了狀態轉換,但保護不了「CAS 失敗之後
    要做什麼」—— 而那條分支會動到錢。

    `event_id` 的唯一索引讓兩者互斥:B 的 INSERT 會等 A 的交易結束,然後 ON CONFLICT
    什麼都不做 → B 直接退場,連讀 order 都不會。Postgres 的唯一索引就是那把鎖,
    不必自己做分散式鎖。
    """

    __tablename__ = "stripe_events"
    __table_args__ = (
        # 保留期清理用(purge_old_stripe_events)。Stripe 最多重送三天,留久一點是為了
        # 對帳,但不能無界成長 —— 這張表每一筆都對應一次真實付款事件。
        Index("ix_stripe_events_received_at", "received_at"),
    )

    #: Stripe 的事件 id(`evt_...`)。**主鍵就是它**,不另外配一個自增 id ——
    #: 去重的語意是「這個外部事件處理過了」,主鍵直接是那個外部識別最不會出錯。
    event_id: Mapped[str] = mapped_column(String(255), primary_key=True)

    #: `payment_intent.succeeded` 之類。只為了事後查帳好讀,不參與任何判斷。
    event_type: Mapped[str] = mapped_column(String(128), nullable=False)

    received_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )
