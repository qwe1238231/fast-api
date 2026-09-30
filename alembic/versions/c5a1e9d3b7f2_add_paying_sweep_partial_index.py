"""add partial index for the abandoned-payment expiry sweep

ix_orders_pending_sweep 的鏡像。那條是 `payment_provider_id IS NULL`,服務 10 分鐘的
T1 過期;這條是 `IS NOT NULL`,服務 PAYMENT_ABANDON_TIMEOUT 的 T2 過期 —— 收掉
「建了 PaymentIntent 然後把分頁關掉」的訂單(Stripe 對這種 intent 不會自動取消、
也不發 webhook,所以只能靠我們掃)。

同樣是 partial + 只有 created_at 一欄:工作集是「此刻正在付款的人」,常態接近零,
索引體積跟在途付款數走,不跟歷史訂單總量走。

Revision ID: c5a1e9d3b7f2
Revises: b3d47f81c026
Create Date: 2026-09-29 15:40:00.000000

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = 'c5a1e9d3b7f2'
down_revision: Union[str, Sequence[str], None] = 'b3d47f81c026'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    # 跟 594fdbaa7d1c 同一個理由:CONCURRENTLY 不能在交易裡跑,autocommit_block 暫時
    # 離開 migration 的交易,建索引時不拿 ACCESS EXCLUSIVE,部署中 order-consumer
    # 的 INSERT 不會被卡住。orders 是最熱的寫入表,這不是可選項。
    with op.get_context().autocommit_block():
        op.create_index(
            "ix_orders_paying_sweep",
            "orders",
            ["created_at"],
            postgresql_where=sa.text("status = 'pending' AND payment_provider_id IS NOT NULL"),
            postgresql_concurrently=True,
            if_not_exists=True,
        )


def downgrade() -> None:
    with op.get_context().autocommit_block():
        op.drop_index(
            "ix_orders_paying_sweep",
            table_name="orders",
            postgresql_concurrently=True,
            if_exists=True,
        )
