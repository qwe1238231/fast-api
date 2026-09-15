"""Transactional Outbox:把「commit 之後欠外部系統的一次動作」寫成 DB 的一列。

解的是 dual-write 問題(第一個案例:expire_pending_orders 的 commit-then-release,
座位票失敗時 nothing repairs this automatically)。核心不變量:**outbox 列與業務變更
在同一個交易裡 INSERT**,所以「訂單 EXPIRED」和「欠一筆座位釋放」保證同生共死 ——
程序在任何一點死掉,待辦都躺在表裡等下一個活著的 relay 來撿。

四個設計決策,連同沒選的那邊:

1. 「已處理」用 processed_at(nullable timestamp)而不是 status 欄位:relay 的查詢
   是 WHERE processed_at IS NULL,partial index 只養未處理的列(同
   ix_orders_pending_sweep 的形狀);時間戳同時回答「什麼時候處理的」,status 欄位
   還得另外加一欄才知道。等真的需要第三種狀態再改,現在只有「欠著/還了」兩種。

2. 排序鍵就是 id(BigInteger sequence):單一 relay 依 id 撈,同一 aggregate 的事件
   天然有序。sequence 不保證 commit 順序(小 id 可能晚 commit)—— 這個縫在單一
   relay + 撈的時候鎖列的做法下無害,細節在 relay 那層處理,不在 schema 層硬扛。

3. attempts + next_attempt_at 不是過度設計,是毒丸防禦:一列 payload 壞了永遠
   處理不完,沒有計數器它會把 relay 卡成無限重試(同 stream 消費者 MAX_DELIVERIES
   的理由);沒有 next_attempt_at,失敗的列會被熱迴圈重打,退避沒地方落腳。

4. 處理完的列**留著等 purge**而不是當場 DELETE:outbox 是修 alert 的第一現場
   (「座位釋放到底帶了什麼參數」),成功就刪等於湮滅現場。量級跟訂單走(每筆
   訂單一兩列),不是 audit log 那種洪水,短保留期的批次 DELETE 承受得起,
   不需要分區。
"""
from datetime import datetime
from typing import Any

from sqlalchemy import BigInteger, DateTime, Index, SmallInteger, String, text
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column
from sqlalchemy.sql import func

from app.db.base import Base

#: event_type 的值域。寫入端(services)與 relay 的 dispatch 表都從這裡拿,
#: 同 audit_log 的 PARTITION_NAME_FORMAT:字串對不上就是靜默漏處理,不能手寫兩份。
SEAT_RELEASE = "seat.release"


class OutboxEntry(Base):
    __tablename__ = "outbox"
    __table_args__ = (
        # relay 的工作集 = 未處理的列,常態接近零。partial index 讓它的體積跟
        # backlog 走,而不是跟歷史總量走;鍵是 id 所以掃出來天然照插入序。
        Index(
            "ix_outbox_unprocessed",
            "id",
            postgresql_where=text("processed_at IS NULL"),
        ),
    )

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)

    #: relay 靠它分派(例:"seat.release")。值的清單活在處理端的 dispatch 表,
    #: 不做 enum —— 加新事件不該動 schema。
    event_type: Mapped[str] = mapped_column(String(64), nullable=False)

    # 指涉哪個業務物件。polymorphic(order/event/...)所以沒有 FK ——
    # 也不需要:同交易寫入保證來源列存在,而 outbox 列本來就該比來源活得短。
    aggregate_type: Mapped[str] = mapped_column(String(32), nullable=False)
    aggregate_id: Mapped[int] = mapped_column(BigInteger, nullable=False)

    #: 處理端需要的全部參數。原則:relay 不回查業務表 —— 來源交易當下的事實
    #: 才是對的(訂單後來被改走,釋放參數不該跟著變)。
    payload: Mapped[dict[str, Any]] = mapped_column(
        JSONB,
        nullable=False,
        default=dict,
    )

    attempts: Mapped[int] = mapped_column(
        SmallInteger,
        nullable=False,
        default=0,
        server_default=text("0"),
    )

    #: 失敗重試的退避落點:relay 只撈 next_attempt_at <= now() 的列,
    #: 失敗一次就把它往後推。server_default now() = 新列立刻可撈。
    next_attempt_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        server_default=func.now(),
        nullable=False,
    )

    processed_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True),
        nullable=True,
    )

    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        server_default=func.now(),
        nullable=False,
    )
