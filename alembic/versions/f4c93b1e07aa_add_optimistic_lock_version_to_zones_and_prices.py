"""add optimistic-lock version columns to zones and event_zone_prices

Revision ID: f4c93b1e07aa
Revises: e7b25c0a9f13
Create Date: 2026-08-19 11:20:00.000000

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = 'f4c93b1e07aa'
down_revision: Union[str, Sequence[str], None] = 'e7b25c0a9f13'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    # 跟 events.version 同一套(見 e7b25c0a9f13):server_default 既是既有列的回填,
    # 也是滾動部署期間舊程式碼 INSERT 不帶這一欄時的來源 —— 所以它是永久的。
    #
    # 粒度是每一列一個版本,而不是把整個 venue / event 的價格表當成一個聚合:
    # 兩個管理員改不同區的票價並沒有真的衝突,共用版本只會製造假的 409。
    for table in ("zones", "event_zone_prices"):
        op.add_column(
            table,
            sa.Column("version", sa.Integer(), nullable=False, server_default=sa.text("1")),
        )


def downgrade() -> None:
    for table in ("zones", "event_zone_prices"):
        op.drop_column(table, "version")
