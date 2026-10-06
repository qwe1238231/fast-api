"""drop the two indexes nobody reads: audit_logs.created_at, events.venue_id

兩條索引都沒有讀者,留著只是寫入成本。「有讀者才建」是這個 repo 的索引規則,先例:
7ae1b044057f 刪 ix_orders_event_id、e5b93c17d4a8 刪 ix_audit_logs_event_type。

ix_audit_logs_created_at:唯一按時間切的讀者是 purge_old_audit_logs,而它自
e5b93c17d4a8 起走 DROP 分區 —— 分區裁剪本身就是那個索引,而且是 O(1) 的。分區那支
migration 把它重建回來是搬表時順手帶的,不是決策。這張表是全系統寫入最重的(每個
稽核事件一筆 INSERT),一條沒人讀的 btree 在每一個分區上各維護一份。

ix_events_venue_id:沒有查詢按 venue_id 過濾(只有 SELECT 它),venues 不刪所以 RI
反查不會發生,events 只有幾百列。成本可忽略,但留著會讓下一個人以為它有讀者。

為什麼不照 f9c14e708b52 那套 CONCURRENTLY:
  - 分區表的索引不支援 DROP INDEX CONCURRENTLY(Postgres 直接拒絕),只能一般 DROP,
    拿 audit_logs 父表 + 每一個分區的 ACCESS EXCLUSIVE。動作本身是目錄操作,毫秒級。
  - events 幾百列,鎖的時間同樣是毫秒。

真正的風險是**等鎖**而不是持鎖:ACCESS EXCLUSIVE 要等所有在途交易放手,等待期間它
排在鎖佇列前面,後面的 INSERT(consume_audit_events 每分鐘一次)全部跟著卡。
lock_timeout 把等待封頂:拿不到就失敗、重跑,不要把 worker 拖死。SET LOCAL 隨交易
結束消失(env.py 把整次 upgrade 包在一個交易裡),不會漏到 session 其他地方。

downgrade 對稱但不對等:分區表上 CREATE INDEX 一樣沒有 CONCURRENTLY,回退會在
audit_logs 上拿鎖重建。回退是罕見路徑,寫明即可。

Revision ID: 9e4d2a7c1f58
Revises: c5a1e9d3b7f2
Create Date: 2026-10-06 10:00:00.000000

"""
from typing import Sequence, Union

from alembic import op


# revision identifiers, used by Alembic.
revision: str = '9e4d2a7c1f58'
down_revision: Union[str, Sequence[str], None] = 'c5a1e9d3b7f2'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.execute("SET LOCAL lock_timeout = '5s'")
    op.drop_index("ix_audit_logs_created_at", table_name="audit_logs")
    op.drop_index("ix_events_venue_id", table_name="events")


def downgrade() -> None:
    op.execute("SET LOCAL lock_timeout = '5s'")
    op.create_index("ix_audit_logs_created_at", "audit_logs", ["created_at"])
    op.create_index("ix_events_venue_id", "events", ["venue_id"])
