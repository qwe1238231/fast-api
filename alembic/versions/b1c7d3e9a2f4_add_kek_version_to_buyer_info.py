"""add buyer_info.kek_version — which KEK wrapped this row's DEK

盤點 A3:PII 金鑰無法線上輪替。密文旁沒有記「這列的 DEK 是用哪一版 KEK 包的」,所以換 KEK
只剩「停機、把所有列重包一次」一條路 —— 不換的話,舊鑰匙一旦外洩就得這樣做,而那時正是最不
能停機的時候。有了版本號,輪替變成線上的:新鑰匙設現役、舊鑰匙退役只用來解,worker 的
rewrap_pii_keks 逐批把舊列的 DEK 重包到新版(只動包裝那一層,密文不碰)。程序在
infra/RUNBOOK.md 情境 E。

SMALLINT NOT NULL DEFAULT 1:migration 之前的列全是第 1 版,當時只有一把鑰匙。PG11+ 的
ADD COLUMN ... DEFAULT 是目錄操作、不重寫表;buyer_info 本來也不大。server_default 同時讓
滾動部署期間舊程式碼的 INSERT(不帶這一欄)照樣成立 —— 守門測試要的就是它。

Revision ID: b1c7d3e9a2f4
Revises: 9e4d2a7c1f58
Create Date: 2026-10-06 16:00:00.000000

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = 'b1c7d3e9a2f4'
down_revision: Union[str, Sequence[str], None] = '9e4d2a7c1f58'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column(
        "buyer_info",
        sa.Column("kek_version", sa.SmallInteger(), nullable=False, server_default="1"),
    )


def downgrade() -> None:
    # 回退後所有列都被當成第 1 版:只有在還沒輪替過(或已經全部重包回第 1 版)時才安全。
    op.drop_column("buyer_info", "kek_version")
