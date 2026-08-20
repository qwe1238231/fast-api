"""tie orders.zone_id to the event's own price row

單欄的 fk_orders_zone_id 只保證「這個 zone 存在」。它擋不住真正會出事的那種列:
event 1(場館 A)的訂單帶著場館 B 的 zone_id。改成指向 event_zone_prices 的複合
主鍵 (event_id, zone_id) 之後,「這一區屬於這場次」變成 Postgres 保證的事。

單欄那條同時被刪掉:event_zone_prices.zone_id 已經指向 zones.id,zone 的存在性
是遞移保證的,留著只是在最熱的寫入路徑上多做一次查表。

NULL 語意靠預設的 MATCH SIMPLE —— zone_id 是 NULL 就整條不檢查,無座位圖的場次
照常運作。

Revision ID: a3d5f81c2b64
Revises: f4c93b1e07aa
Create Date: 2026-08-20 10:40:00.000000

"""
from typing import Sequence, Union

from alembic import op


# revision identifiers, used by Alembic.
revision: str = 'a3d5f81c2b64'
down_revision: Union[str, Sequence[str], None] = 'f4c93b1e07aa'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None

BACKWARD_INCOMPATIBLE = """\
新增外鍵,舊程式碼理論上可能寫出違規列 —— 記在這裡是因為
test_new_migrations_survive_a_rolling_deploy 看不到它:那條規則是 AST 比對
`op.<name>()`,而這裡用的是 op.execute 的原生 SQL(為了 NOT VALID)。守門測試
的盲點不該變成沒人寫下的風險。

判定是安全的:訂單的 zone_id 只從 pricing 的路徑來,而那條路徑本身就是讀
event_zone_prices 得到的,不可能指到沒有價格列的 zone。舊 task 造不出違規列。\
"""


def upgrade() -> None:
    # 兩段式,不用 op.create_foreign_key —— 它產生的是會立刻驗證的 ADD CONSTRAINT,
    # 那會全表掃 orders 並在整段期間持有 SHARE ROW EXCLUSIVE,擋掉所有下單。
    #
    # NOT VALID:只登記約束、跳過既有列的檢查,毫秒級。**新寫入從這一刻起就受約束**
    # ,所以就算兩段之間中斷也不會留下洞。
    op.execute(
        "ALTER TABLE orders ADD CONSTRAINT fk_orders_event_zone_price "
        "FOREIGN KEY (event_id, zone_id) "
        "REFERENCES event_zone_prices (event_id, zone_id) NOT VALID"
    )
    # VALIDATE 才回頭掃既有列,但只拿 SHARE UPDATE EXCLUSIVE —— 不擋讀也不擋寫。
    op.execute("ALTER TABLE orders VALIDATE CONSTRAINT fk_orders_event_zone_price")
    op.drop_constraint("fk_orders_zone_id", "orders", type_="foreignkey")


def downgrade() -> None:
    # 先把舊的單欄外鍵裝回去再拆新的,中間不留下「zone_id 完全沒有外鍵」的窗口。
    op.create_foreign_key(
        "fk_orders_zone_id", "orders", "zones", ["zone_id"], ["id"]
    )
    op.drop_constraint("fk_orders_event_zone_price", "orders", type_="foreignkey")
