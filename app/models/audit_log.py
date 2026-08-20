"""稽核紀錄。**按月 RANGE 分區**,分區鍵是 created_at。

分區的理由不是「無界成長」—— 保留機制一直都在(purge_old_audit_logs,90 天)。
理由是那個保留機制原本的形狀:一次 `DELETE ... WHERE created_at < cutoff` 把整批
舊列刪掉,WAL 暴衝、複寫延遲,而且死列要等 autovacuum 慢慢回收,表在磁碟上只會
愈來愈鬆。分區之後同一件事是 `DROP TABLE` 一個子表 —— O(1) 的目錄操作,不產生
WAL 也不需要 vacuum。

三個非顯而易見的後果,都在下面的程式碼裡:
  1. 分區表的主鍵**必須包含分區鍵**,所以 PK 是 (id, created_at) 而不是 (id)
  2. created_at 是**事件發生時間**(從 stream 帶進來),不是寫入時間 —— 延遲很久的
     entry 可能落在沒有對應分區的月份,所以一定要有 DEFAULT 分區兜底
  3. 分區要**事先**建好。DEFAULT 一旦收了某個月的列,再想為那個月建分區會被
     Postgres 拒絕(既有列會違反新分區的約束)
"""
from datetime import datetime
from typing import Any

from sqlalchemy import (
    DDL,
    BigInteger,
    DateTime,
    ForeignKey,
    Index,
    String,
    event,
)
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column
from sqlalchemy.sql import func

from app.db.base import Base

#: DEFAULT 分區的名字。purge 永遠不會刪它 —— 它是「沒有對應月份分區的列」的去處,
#: 空的時候不佔成本,不空的時候是一個需要有人去看的訊號。
DEFAULT_PARTITION = "audit_logs_default"

#: 月分區的命名格式。purge 與建立分區的 cron 都靠這個格式互相認得對方建的東西,
#: 所以它是**單一來源**,不要在別處手寫 f"audit_logs_{...}"。
PARTITION_NAME_FORMAT = "audit_logs_{year:04d}_{month:02d}"


def partition_name(year: int, month: int) -> str:
    return PARTITION_NAME_FORMAT.format(year=year, month=month)


class AuditLog(Base):
    __tablename__ = "audit_logs"
    __table_args__ = (
        # event_type 的單欄索引**刻意不留**:全專案沒有任何讀取端(只有 worker 寫入
        # 與 purge),它現在純粹是每一筆 INSERT 的額外成本。等真的有查詢端再依實際
        # pattern 建複合索引,而不是先猜。
        #
        # actor_user_id 的索引則保留 —— 它不是為了查詢,是為了 fk_audit_logs_actor
        # _user_id 的 ON DELETE SET NULL:刪一個 user 時 Postgres 要找出所有指向他的
        # 列,沒有索引就是掃過**每一個分區**。
        Index("ix_audit_logs_actor_user_id", "actor_user_id"),
        Index("ix_audit_logs_created_at", "created_at"),
        {"postgresql_partition_by": "RANGE (created_at)"},
    )

    # 複合主鍵不是設計偏好,是 Postgres 的硬性要求:分區表的唯一約束必須包含分區鍵,
    # 否則跨分區的唯一性無法在不掃全表的情況下保證。id 仍然由 sequence 產生,
    # 全域唯一性實際上還是 id 一個人扛 —— created_at 只是被拉進來湊約束。
    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)

    event_type: Mapped[str] = mapped_column(String(64), nullable=False)

    # SET NULL:稽核紀錄必須比它記錄的那個人活得久 —— 「誰在什麼時候改了什麼」
    # 的價值恰恰在事後,而 CASCADE 會讓「刪掉帳號」變成「湮滅自己的紀錄」。
    # 這一欄本來就 nullable(系統自身的動作沒有 actor),所以 SET NULL 不需要
    # 任何 schema 讓步:actor 消失了,事件本身還在。
    actor_user_id: Mapped[int | None] = mapped_column(
        ForeignKey("users.id", ondelete="SET NULL", name="fk_audit_logs_actor_user_id"),
        nullable=True,
    )

    actor_ip: Mapped[str | None] = mapped_column(
        String(45),  # Supports IPv4 and IPv6
        nullable=True,
    )

    target_type: Mapped[str | None] = mapped_column(
        String(32),
        nullable=True,
    )

    # 長度上限趁重建表一起加上。值一律是 str(<int id>)(見各端點的 target_id=),
    # 64 綽綽有餘。單獨為它跑一次 ALTER TYPE 不划算(要在 audit_logs 上拿
    # ACCESS EXCLUSIVE 做驗證掃描),但這支 migration 本來就要重建整張表。
    target_id: Mapped[str | None] = mapped_column(
        String(64),
        nullable=True,
    )

    payload: Mapped[dict[str, Any] | None] = mapped_column(
        JSONB,
        nullable=True,
        default=dict,
    )

    success: Mapped[bool] = mapped_column(
        nullable=False,
        default=True,
    )
    error_code: Mapped[str | None] = mapped_column(
        String(64),
        nullable=True,
    )

    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        server_default=func.now(),
        nullable=False,
        primary_key=True,
    )


# 分區表在沒有任何分區時,每一筆 INSERT 都會噴「no partition of relation found」。
# 測試是走 metadata.create_all 建表的(不經過 migration),所以那條路徑也必須拿得到
# 一個能收下所有列的分區 —— 掛一個 after_create 建 DEFAULT。
#
# 生產環境的月分區由 worker 的 ensure_audit_log_partitions 事先建好;DEFAULT 在那裡
# 的角色不同,是「月分區沒建成功」時的安全網,而不是常態去處。
event.listen(
    AuditLog.__table__,
    "after_create",
    DDL(
        f"CREATE TABLE IF NOT EXISTS {DEFAULT_PARTITION} "
        f"PARTITION OF audit_logs DEFAULT"
    ),
)
