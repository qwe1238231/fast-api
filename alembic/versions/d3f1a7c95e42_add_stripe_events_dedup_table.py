"""add stripe_events dedup table

Revision ID: d3f1a7c95e42
Revises: c41f8a2d5b70
Create Date: 2026-08-17 16:40:00.000000

純新增(expand):只建一張新表,舊程式碼完全不碰它,所以滾動部署期間新舊 task 共存
是安全的 —— 不需要 BACKWARD_INCOMPATIBLE 的說明。
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = "d3f1a7c95e42"
down_revision: Union[str, Sequence[str], None] = "c41f8a2d5b70"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    """Upgrade schema."""
    op.create_table(
        "stripe_events",
        # 主鍵直接是 Stripe 的事件 id。這張表的唯一索引同時是「同一個事件只處理一次」
        # 的那把鎖 —— 並發重送時第二個 INSERT 會等第一個交易結束,然後什麼都不做。
        sa.Column("event_id", sa.String(length=255), nullable=False),
        sa.Column("event_type", sa.String(length=128), nullable=False),
        sa.Column(
            "received_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.PrimaryKeyConstraint("event_id"),
    )
    op.create_index(
        "ix_stripe_events_received_at", "stripe_events", ["received_at"], unique=False
    )


def downgrade() -> None:
    """Downgrade schema."""
    op.drop_index("ix_stripe_events_received_at", table_name="stripe_events")
    op.drop_table("stripe_events")
