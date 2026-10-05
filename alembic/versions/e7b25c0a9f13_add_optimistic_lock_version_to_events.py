"""add an optimistic-lock version column to events

Revision ID: e7b25c0a9f13
Revises: d3f1a7c95e42
Create Date: 2026-08-19 09:40:00.000000

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = 'e7b25c0a9f13'
down_revision: Union[str, Sequence[str], None] = 'd3f1a7c95e42'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    # Event.__mapper_args__["version_id_col"] 的後端欄位。後台編輯的 lost update
    # 防線:兩個管理員同時開編輯頁時,後存檔的那份不再靜默蓋掉先存檔的改動。
    #
    # server_default 有兩個作用,兩個都不能省:
    #   1. 回填 —— 對已有資料的表加 NOT NULL 欄位,沒有 default 會直接失敗。
    #   2. 滾動部署 —— migration 先跑、舊程式碼還在服務,而舊程式碼的
    #      INSERT INTO events 不會帶 version 這一欄,得由資料庫補值。
    #      (所以這個 default 是永久的,不要在後續 migration 裡拿掉。)
    #
    # PG 11+ 起,帶「常數」default 的 ADD COLUMN 是純 metadata 操作,不重寫整張表;
    # 仍會短暫取得 ACCESS EXCLUSIVE lock,但 events 是低寫入量的表,不需要
    # CONCURRENTLY 那套(而 ADD COLUMN 本來也沒有 CONCURRENTLY)。
    op.add_column(
        "events",
        sa.Column("version", sa.Integer(), nullable=False, server_default=sa.text("1")),
    )


def downgrade() -> None:
    op.drop_column("events", "version")
