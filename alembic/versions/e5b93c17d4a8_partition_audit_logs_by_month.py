"""partition audit_logs by month

保留機制一直都在(purge_old_audit_logs,90 天),所以這支 migration 解的不是
「無界成長」,而是**保留機制原本的形狀**:

    DELETE FROM audit_logs WHERE created_at < cutoff

單一交易、無批次。一次刪掉整批舊列會讓 WAL 暴衝、複寫延遲,而且死列要等
autovacuum 慢慢回收 —— 表在磁碟上不會縮。按月分區之後,同一件事變成 DROP 一個
子表:O(1) 的目錄操作,不產生 WAL 也不需要 vacuum。

Postgres 不能就地把普通表轉成分區表,所以走「建新表 → 搬資料 → 換名」。現在這張
表還小,所以停寫窗口是毫秒級 —— 這正是把它排進「趁早做」那一批的理由。

順帶(都需要重建表才划算,單獨做不值得):
  - target_id 從無長度 VARCHAR 收成 VARCHAR(64)。值一律是 str(<int id>)。
    **如果既有資料有超過 64 的值,這裡會失敗** —— 那是對的,靜默截斷稽核資料
    比 migration 失敗糟糕得多。
  - 砍掉 ix_audit_logs_event_type:全專案沒有讀取端,它只是每筆 INSERT 的成本。
    actor_user_id 的索引保留,理由不是查詢而是 ON DELETE SET NULL —— 刪 user 時
    Postgres 要找出指向他的列,沒索引就是掃過每一個分區。

Revision ID: e5b93c17d4a8
Revises: d1a75f3c8e20
Create Date: 2026-08-20 14:20:00.000000

"""
from datetime import date
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = 'e5b93c17d4a8'
down_revision: Union[str, Sequence[str], None] = 'd1a75f3c8e20'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None

BACKWARD_INCOMPATIBLE = """\
重建 audit_logs 並換名。滾動部署期間舊 task 仍在寫這張表,而 rename 那一刻的
in-flight INSERT 會失敗。

判定可接受:寫入端只有 worker 的 consume_audit_events,它從 Redis Stream 讀、
失敗就不 ack —— 事件留在 stream 裡,下一輪(一分鐘後)重放。換句話說這條路徑
本來就設計成可重試的,而 rename 的窗口是毫秒級。API 沒有任何一條路徑直接寫
audit_logs(都走 emit_event 進 stream)。

另外 target_id 收成 VARCHAR(64) 對舊程式碼是收緊 —— 但寫進去的值一律是
str(<int id>),不可能超過。\
"""

_COLUMNS = (
    "id, event_type, actor_user_id, actor_ip, target_type, target_id, "
    "payload, success, error_code, created_at"
)


def _month_bounds(year: int, month: int) -> tuple[str, str]:
    """回傳半開區間 [start, end) 的兩個日期字串。"""
    start = date(year, month, 1)
    end = date(year + 1, 1, 1) if month == 12 else date(year, month + 1, 1)
    return start.isoformat(), end.isoformat()


def _create_partition(table: str, year: int, month: int) -> None:
    start, end = _month_bounds(year, month)
    name = f"audit_logs_{year:04d}_{month:02d}"
    op.execute(
        f"CREATE TABLE {name} PARTITION OF {table} "
        f"FOR VALUES FROM ('{start}') TO ('{end}')"
    )


def upgrade() -> None:
    bind = op.get_bind()

    # 既有資料涵蓋哪些月份 —— 這些分區必須先建好,否則搬過去的列會全部落進
    # DEFAULT,而落進 DEFAULT 的列有兩個壞處:purge 按月 DROP 時碰不到它們,
    # 而且之後想補建那個月的分區會被 Postgres 拒絕(既有列違反新分區約束)。
    months = [
        (row[0], row[1])
        for row in bind.execute(
            sa.text(
                "SELECT DISTINCT EXTRACT(YEAR FROM created_at)::int, "
                "EXTRACT(MONTH FROM created_at)::int "
                "FROM audit_logs ORDER BY 1, 2"
            )
        )
    ]
    # 加上當月與接下來兩個月,讓 worker 的 ensure_audit_log_partitions 有東西可以
    # 接手,也避免部署完到第一次 cron 之間的空窗全部掉進 DEFAULT。
    today = date.today()
    for offset in range(3):
        year, month = divmod(today.month - 1 + offset, 12)
        months.append((today.year + year, month + 1))
    months = sorted(set(months))

    # sequence 先脫離舊表,否則等一下 DROP TABLE 會把它一起帶走(OWNED BY)。
    # 序列值必須延續 —— 重新從 1 開始的話,新舊 id 會撞在一起。
    op.execute("ALTER SEQUENCE audit_logs_id_seq OWNED BY NONE")

    # 用暫時的名字建,最後才換名。直接叫 audit_logs 的話,主鍵索引會跟舊表的
    # audit_logs_pkey 撞名 —— 索引名在 Postgres 是 schema 層級全域的,約束名才是
    # 每張表各自獨立。
    op.execute(
        f"""
        CREATE TABLE audit_logs_new (
            id BIGINT NOT NULL DEFAULT nextval('audit_logs_id_seq'),
            event_type VARCHAR(64) NOT NULL,
            actor_user_id INTEGER,
            actor_ip VARCHAR(45),
            target_type VARCHAR(32),
            target_id VARCHAR(64),
            payload JSONB,
            success BOOLEAN NOT NULL,
            error_code VARCHAR(64),
            created_at TIMESTAMP WITH TIME ZONE NOT NULL DEFAULT now(),
            PRIMARY KEY (id, created_at)
        ) PARTITION BY RANGE (created_at)
        """
    )
    op.execute(
        "CREATE TABLE audit_logs_default PARTITION OF audit_logs_new DEFAULT"
    )
    for year, month in months:
        _create_partition("audit_logs_new", year, month)

    op.execute(
        f"INSERT INTO audit_logs_new ({_COLUMNS}) "
        f"SELECT {_COLUMNS} FROM audit_logs"
    )
    op.execute("DROP TABLE audit_logs")
    op.execute("ALTER TABLE audit_logs_new RENAME TO audit_logs")
    op.execute("ALTER INDEX audit_logs_new_pkey RENAME TO audit_logs_pkey")

    # 索引與外鍵留到搬完才建:舊表已經 DROP,名字空出來了,而且對著空表建比
    # 邊搬邊維護便宜。分區表上建的索引會自動下推到每一個分區。
    op.create_index("ix_audit_logs_actor_user_id", "audit_logs", ["actor_user_id"])
    op.create_index("ix_audit_logs_created_at", "audit_logs", ["created_at"])
    op.execute(
        "ALTER TABLE audit_logs ADD CONSTRAINT fk_audit_logs_actor_user_id "
        "FOREIGN KEY (actor_user_id) REFERENCES users (id) ON DELETE SET NULL"
    )
    op.execute("ALTER SEQUENCE audit_logs_id_seq OWNED BY audit_logs.id")


def downgrade() -> None:
    op.execute("ALTER SEQUENCE audit_logs_id_seq OWNED BY NONE")
    op.execute(
        f"""
        CREATE TABLE audit_logs_old (
            id BIGINT NOT NULL DEFAULT nextval('audit_logs_id_seq'),
            event_type VARCHAR(64) NOT NULL,
            actor_user_id INTEGER,
            actor_ip VARCHAR(45),
            target_type VARCHAR(32),
            target_id VARCHAR(64),
            payload JSONB,
            success BOOLEAN NOT NULL,
            error_code VARCHAR(64),
            created_at TIMESTAMP WITH TIME ZONE NOT NULL DEFAULT now(),
            PRIMARY KEY (id)
        )
        """
    )
    op.execute(
        f"INSERT INTO audit_logs_old ({_COLUMNS}) "
        f"SELECT {_COLUMNS} FROM audit_logs"
    )
    # DROP 分區表會連同所有子分區一起走。
    op.execute("DROP TABLE audit_logs")
    op.execute("ALTER TABLE audit_logs_old RENAME TO audit_logs")
    op.execute("ALTER INDEX audit_logs_old_pkey RENAME TO audit_logs_pkey")
    op.create_index("ix_audit_logs_actor_user_id", "audit_logs", ["actor_user_id"])
    op.create_index("ix_audit_logs_created_at", "audit_logs", ["created_at"])
    op.create_index("ix_audit_logs_event_type", "audit_logs", ["event_type"])
    op.execute(
        "ALTER TABLE audit_logs ADD CONSTRAINT fk_audit_logs_actor_user_id "
        "FOREIGN KEY (actor_user_id) REFERENCES users (id) ON DELETE SET NULL"
    )
    op.execute("ALTER SEQUENCE audit_logs_id_seq OWNED BY audit_logs.id")
