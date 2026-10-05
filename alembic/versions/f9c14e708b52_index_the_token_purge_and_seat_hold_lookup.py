"""index the refresh-token purge and the seat-hold lookup

兩條都是 EXPLAIN (ANALYZE, BUFFERS) 在 200 萬訂單 / 50 萬 token / 10 萬 hold 的
資料集上量出來的,不是猜的。

1. purge_expired_refresh_tokens 每晚整張表 seq scan
     before: Seq Scan, Rows Removed by Filter: 500000, 9615 blocks, 76.9 ms
             ——**刪掉 0 列**,而成本隨表線性成長
     after : BitmapOr(兩個 Bitmap Index Scan), 4 blocks, 0.13 ms

   revoked_at 用 partial(WHERE revoked_at IS NOT NULL):絕大多數 token 從未被
   撤銷,NULL 的列對這個查詢沒有意義。索引 3.4 MB → 8 KB。

   附帶澄清一個常見誤解:OR **不會**讓索引失效。Postgres 用 BitmapOr 把兩邊的
   索引掃描合起來,前提只是兩邊各自有索引可用 —— 不需要把 DELETE 拆成兩句。

2. seat_holds 的 (event_id, block_id) 等值查詢只有 EXCLUDE 的 GiST 可用
     before: Bitmap Index Scan on ex_seat_holds_no_overlap
             rows=120(實際只要 20), 63 blocks, 0.41 ms
     after : Index Scan, rows=20, 42 blocks, 0.049 ms

   GiST 對等值是有損的:它先給出一組候選再由 recheck 篩。專用 btree 精準命中,
   而且體積只有四分之一(2.2 MB vs 8.2 MB)。

刻意**沒有**加的第三條:ix_orders_active 的 INCLUDE (user_id, quantity) 能讓對帳
查詢變成 Index Only Scan(4006 → 23 blocks),但索引要從 8.3 MB 漲到 47 MB,而測到的
收益偏樂觀 —— benchmark 刻意把同場次的訂單打散,真實的開賣爆量會讓它們實體上聚在
一起,heap block 數本來就少。那條要看實際的售票形態再決定。

Revision ID: f9c14e708b52
Revises: e5b93c17d4a8
Create Date: 2026-08-24 10:30:00.000000

"""
from typing import Sequence, Union

from alembic import op


# revision identifiers, used by Alembic.
revision: str = 'f9c14e708b52'
down_revision: Union[str, Sequence[str], None] = 'e5b93c17d4a8'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


#: (name, table, definition tail)
_INDEXES: tuple[tuple[str, str, str], ...] = (
    (
        "ix_refresh_tokens_absolute_expires_at",
        "refresh_tokens",
        "(absolute_expires_at)",
    ),
    (
        "ix_refresh_tokens_revoked_at",
        "refresh_tokens",
        "(revoked_at) WHERE revoked_at IS NOT NULL",
    ),
    ("ix_seat_holds_event_block", "seat_holds", "(event_id, block_id)"),
)


def upgrade() -> None:
    # CONCURRENTLY:一般的 CREATE INDEX 會拿 SHARE lock,擋掉整張表的寫入直到建完。
    # refresh_tokens 的寫入是**登入路徑**,seat_holds 的寫入是**下單路徑** —— 兩條
    # 都不能為了建索引停下來。代價是 CONCURRENTLY 不能在交易裡跑,所以要 autocommit。
    #
    # IF NOT EXISTS 是搭配 CONCURRENTLY 的必需品,不是保險:CONCURRENTLY 失敗會留下
    # 一個 INVALID 的索引殘骸,重跑 migration 時得能跳過它。真的遇到的話要先手動
    # DROP INDEX 那個 invalid 的,再重跑 —— 光靠 IF NOT EXISTS 不會把它修好。
    with op.get_context().autocommit_block():
        for name, table, tail in _INDEXES:
            op.execute(
                f"CREATE INDEX CONCURRENTLY IF NOT EXISTS {name} ON {table} {tail}"
            )


def downgrade() -> None:
    with op.get_context().autocommit_block():
        for name, _table, _tail in reversed(_INDEXES):
            op.execute(f"DROP INDEX CONCURRENTLY IF EXISTS {name}")
