"""widen the high-churn primary keys to BIGINT

int4 的 21 億上限對這四張表不是理論值 —— 而且**序列值是消耗掉就不回來的**:
刪掉的列不歸還,失敗的 INSERT 也照吃。

    refresh_tokens  最快。每次登入一條,之後每次 rotation 再一條,清理 job 刪掉的
                    列不會把序列值還回來。100 萬登入/天 × 10 次輪替 ≈ 1000 萬/天,
                    int4 撐約 210 天。
    audit_logs      次快,而且是全庫唯一真正無界成長的那張。
    orders          含 idempotency 衝突與各種被擋下的下單所吃掉的序列值。
    seat_holds      跟著有座位的訂單走,速率與 orders 同級。

其餘的表(venues/zones/seat_blocks/seats/events/users/buyer_info)刻意維持 int4:
它們是有界的目錄型資料,1000 個場館 × 5 萬席也才 5000 萬。跟著變寬只會讓每一列與
每一個索引項多背 4 bytes。

**這支 migration 的重點在 ALTER SEQUENCE,不在 ALTER COLUMN。**
改欄位型別不會動到它所擁有的 sequence(實測:ALTER TABLE ... TYPE BIGINT 之後
pg_sequences 仍然是 data_type=integer / max_value=2147483647)。只寫 ALTER COLUMN
的版本看起來做完了,實際上到 21 億照樣噴 nextval: reached maximum value of sequence
—— 一支什麼都沒修好卻顯示成功的 migration。

Revision ID: d1a75f3c8e20
Revises: c8f4a2e6b193
Create Date: 2026-08-20 13:30:00.000000

"""
from typing import Sequence, Union

from alembic import op


# revision identifiers, used by Alembic.
revision: str = 'd1a75f3c8e20'
down_revision: Union[str, Sequence[str], None] = 'c8f4a2e6b193'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


#: 主鍵要加寬的表。順序有意義:被參照的 orders / refresh_tokens 先改,
#: 指向它們的欄位(下面 _REFERENCING)後改。
_TABLES: tuple[str, ...] = ("refresh_tokens", "audit_logs", "orders", "seat_holds")

#: (table, column) —— 指向上面那些主鍵的外鍵欄位,型別必須跟著走。
#: 兩端型別不一致的話 RI 檢查要隱式轉型,而那會讓索引用不上。
_REFERENCING: tuple[tuple[str, str], ...] = (
    ("refresh_tokens", "parent_id"),   # 自我外鍵
    ("seat_holds", "order_id"),
)


def upgrade() -> None:
    # ALTER COLUMN TYPE 會全表重寫並持有 ACCESS EXCLUSIVE。現在四張表都還很小
    # (毫秒級),而這正是這支 migration 要趁現在做的唯一理由:同樣的操作在一億
    # 列的 orders 上是幾十分鐘的停機。
    for table in _TABLES:
        op.execute(f"ALTER TABLE {table} ALTER COLUMN id TYPE BIGINT")
        # 真正解決問題的是這一行。少了它,欄位是 bigint 但序列仍在 21 億封頂。
        op.execute(f"ALTER SEQUENCE {table}_id_seq AS BIGINT")

    for table, column in _REFERENCING:
        op.execute(f"ALTER TABLE {table} ALTER COLUMN {column} TYPE BIGINT")


def downgrade() -> None:
    # 注意:如果此時已經有超過 int4 範圍的 id,這裡會失敗 —— 而那是對的行為,
    # 靜默截斷主鍵比 migration 退不回去糟糕得多。
    for table, column in reversed(_REFERENCING):
        op.execute(f"ALTER TABLE {table} ALTER COLUMN {column} TYPE INTEGER")

    for table in reversed(_TABLES):
        op.execute(f"ALTER SEQUENCE {table}_id_seq AS INTEGER")
        op.execute(f"ALTER TABLE {table} ALTER COLUMN id TYPE INTEGER")
