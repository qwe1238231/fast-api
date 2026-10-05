"""make ix_orders_active cover the inventory reconcile query

`ix_orders_active (event_id, status)` 只有鍵欄位,而對帳查詢要的是 quantity 與
user_id —— 所以每一列都得回表。加上 INCLUDE 之後變成 Index Only Scan。

重點不是單次查詢快 20ms。detect_inventory_drift **每 5 分鐘對每一個 published
場次各跑一次** compute_expected_available(worker.py 的迴圈),所以真正的成本是
「場次數 × 每場 heap block」:

    訂單實體打散(普通場次、長售期)   4000 blocks/場 → 300 場一輪 ≈ 9.4 GB
    訂單實體集中(熱門場次爆量)         58 blocks/場 → 一輪 ≈ 136 MB
    打散 + INCLUDE                      23 blocks/場 → 一輪 ≈ 54 MB

打散那一列的問題不是慢,是每 5 分鐘把 shared_buffers 沖一遍 —— 排擠掉的正是下單
路徑需要的頁面。熱門場次自己用不到這個索引,但普通場次會一直在背景製造那個負載。

成本:索引 8.3 MB → 47 MB(以 100 萬筆持有中的訂單量)。寫入放大幾乎是零 ——
user_id 與 quantity 建立後不再改(CAS 只動 status 與時間戳),而 HOT 早就因為
status 在索引裡而打斷了。

Revision ID: b64d2af9137e
Revises: f9c14e708b52
Create Date: 2026-08-24 15:10:00.000000

"""
from typing import Sequence, Union

from alembic import op


# revision identifiers, used by Alembic.
revision: str = 'b64d2af9137e'
down_revision: Union[str, Sequence[str], None] = 'f9c14e708b52'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None

_PREDICATE = "WHERE status IN ('pending', 'paid', 'confirmed')"
_TMP = "ix_orders_active_covering"


def upgrade() -> None:
    # 先建新的、再拆舊的、最後改名。順序是刻意的:反過來(先 drop 再 create)會留下
    # 一段沒有索引的窗口,而在那段時間裡對帳查詢會退化成 orders 全表掃 —— 200 萬列。
    #
    # 用暫時的名字建,因為索引名在 Postgres 是 schema 層級全域的,同名兩個並存不了。
    # 這個順序也比較耐失敗:中斷在 create 之後,兩個索引並存(都可用、都有效),
    # 重跑一次就會接著把舊的拆掉並改名。
    with op.get_context().autocommit_block():
        op.execute(
            f"CREATE INDEX CONCURRENTLY IF NOT EXISTS {_TMP} ON orders "
            f"(event_id, status) INCLUDE (user_id, quantity) {_PREDICATE}"
        )
        op.execute("DROP INDEX CONCURRENTLY IF EXISTS ix_orders_active")
        # 純目錄操作(微秒級),但仍會拿 ACCESS EXCLUSIVE —— 所以放在最後,
        # 而不是夾在兩個耗時的 CONCURRENTLY 中間。
        op.execute(f"ALTER INDEX {_TMP} RENAME TO ix_orders_active")


def downgrade() -> None:
    with op.get_context().autocommit_block():
        op.execute(
            f"CREATE INDEX CONCURRENTLY IF NOT EXISTS {_TMP} ON orders "
            f"(event_id, status) {_PREDICATE}"
        )
        op.execute("DROP INDEX CONCURRENTLY IF EXISTS ix_orders_active")
        op.execute(f"ALTER INDEX {_TMP} RENAME TO ix_orders_active")
