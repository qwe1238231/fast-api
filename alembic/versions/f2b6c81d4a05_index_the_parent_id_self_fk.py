"""index the refresh_tokens.parent_id self-FK for RI checks

parent_id 是純寫入欄位(rotation 設、沒有任何讀取端),但它是自我外鍵 —— 每刪一列
parent,RI trigger 都要查一次「還有沒有人指著我」。沒有索引時那是**每列一次 seq
scan**,而 purge_expired 每晚在做的正是成批刪除。

實測(50 萬 token,刪 2000 對 parent+child):

    無索引   Trigger for constraint refresh_tokens_parent_id_fkey:
             time=57423ms calls=4000        → 整句 57.5 秒
    有索引   time=12ms calls=4000           → 13.7 ms(4200 倍)

partial(WHERE parent_id IS NOT NULL,略過每個 family 的頭)可用於 RI:planner 能從
RI 查詢的 `parent_id = $1` 推出 IS NOT NULL —— 上面那組數字就是用 partial 量的。

沒有選「乾脆刪掉 parent_id」:family_id + created_at 只能重建線性的 rotation 鏈,
而 REFRESH_TOKEN_REUSE_GRACE_SECONDS > 0 時同一個 parent 可以有多個 child(寬限期
內重用),樹狀的分支只有 parent_id 記得 —— 那正是安全事件鑑識時要看的東西。

Revision ID: f2b6c81d4a05
Revises: e8f0a3d5c927
Create Date: 2026-08-24 22:40:00.000000

"""
from typing import Sequence, Union

from alembic import op


# revision identifiers, used by Alembic.
revision: str = 'f2b6c81d4a05'
down_revision: Union[str, Sequence[str], None] = 'e8f0a3d5c927'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    # CONCURRENTLY + autocommit:refresh_tokens 的寫入是登入路徑,理由同 f9c14e708b52。
    with op.get_context().autocommit_block():
        op.execute(
            "CREATE INDEX CONCURRENTLY IF NOT EXISTS ix_refresh_tokens_parent_id "
            "ON refresh_tokens (parent_id) WHERE parent_id IS NOT NULL"
        )


def downgrade() -> None:
    with op.get_context().autocommit_block():
        op.execute("DROP INDEX CONCURRENTLY IF EXISTS ix_refresh_tokens_parent_id")
