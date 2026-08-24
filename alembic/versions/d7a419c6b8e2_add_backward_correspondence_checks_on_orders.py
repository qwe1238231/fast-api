"""add the backward-correspondence CHECKs on orders

orders 已經有 forward correspondence(status='paid' → paid_at IS NOT NULL 那四條)。
反過來那一半原本沒有,所以資料庫容得下狀態機到不了的列:

    expired_at 與 cancelled_at 同時有值   —— 三個終態互斥,到得了一個就到不了另一個
    confirmed_at 有值但 paid_at 是 NULL   —— CONFIRMED 只能從 PAID 來
    confirmed_at 早於 paid_at             —— 先確認後付款

這種列不會讓任何程式碼出錯。它只會讓對帳報表把同一筆算兩次,而那種錯誤沒有人會在
當下發現 —— 這正是它值得由資料庫擋的理由。

**刻意沒有**加「expired/cancelled 就不可能有 paid_at」。今天的狀態機確實保證了
(_VALID_TRANSITIONS 裡 PAID 只能往 CONFIRMED),但那是一條會變的營運規則:要支援
已付款訂單的退款或作廢時,PAID → CANCELLED 就會變合法,而那時這條 CHECK 會變成擋路
的東西。上面三條則是不管退款怎麼做都成立。

Revision ID: d7a419c6b8e2
Revises: c3e8b5f2091d
Create Date: 2026-08-24 20:15:00.000000

"""
from typing import Sequence, Union

from alembic import op


# revision identifiers, used by Alembic.
revision: str = 'd7a419c6b8e2'
down_revision: Union[str, Sequence[str], None] = 'c3e8b5f2091d'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None

BACKWARD_INCOMPATIBLE = """\
新增 CHECK,滾動部署期間舊 task 理論上可能寫出違規列。

判定安全:寫這四個時間戳的路徑只有 crud/order.py 的 transition_order_status,
而它從 _STATUS_TIMESTAMP 取單一欄位、且轉換合法性由 _VALID_TRANSITIONS 先擋過。
舊程式碼跟新程式碼在這件事上完全一樣 —— 這三條 CHECK 表達的正是那個狀態機已經
保證的東西,只是把它從 Python 搬到資料庫。\
"""

_CONSTRAINTS: tuple[tuple[str, str], ...] = (
    ("ck_orders_terminal_exclusive", "expired_at IS NULL OR cancelled_at IS NULL"),
    ("ck_orders_confirmed_needs_paid", "confirmed_at IS NULL OR paid_at IS NOT NULL"),
    ("ck_orders_paid_before_confirmed", "confirmed_at IS NULL OR paid_at <= confirmed_at"),
)


def upgrade() -> None:
    # NOT VALID + VALIDATE:一般的 ADD CHECK 會全表掃 orders 並在整段期間持有
    # ACCESS EXCLUSIVE。events 那次用一般寫法是因為它是小表,orders 不是(200 萬列)。
    for name, condition in _CONSTRAINTS:
        op.execute(
            f"ALTER TABLE orders ADD CONSTRAINT {name} CHECK ({condition}) NOT VALID"
        )
        op.execute(f"ALTER TABLE orders VALIDATE CONSTRAINT {name}")


def downgrade() -> None:
    for name, _ in reversed(_CONSTRAINTS):
        op.drop_constraint(name, "orders", type_="check")
