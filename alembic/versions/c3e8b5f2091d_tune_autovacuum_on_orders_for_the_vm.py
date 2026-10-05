"""tune autovacuum on orders so the visibility map stays current

**沒有這一步,上一支 migration 的 INCLUDE 覆蓋索引是白加的。**

Index Only Scan 需要 visibility map 標記頁面 all-visible,而只有 vacuum 會設那個
位元。實測(200 萬列,對帳查詢 compute_expected_available):

    VACUUM 之後     VM 100%    heap fetches   0     22 blocks    0.28 ms
    +4 萬筆未清      VM  98%    heap fetches  81    103 blocks    0.48 ms
    +40 萬筆未清     VM  83%    heap fetches 801    827 blocks    1.90 ms

40 萬正好是**預設值**的觸發點(autovacuum_vacuum_insert_threshold 1000 +
insert_scale_factor 0.2 × 200 萬)。也就是說什麼都不調的話,穩態就是最後那一列 ——
覆蓋索引的效果被吃掉 37 倍,而 detect_inventory_drift 每 5 分鐘對每一場都跑一次。

兩個判斷寫在這裡,因為它們是這組數字的理由:

  scale_factor 一律設 0、只留固定 threshold。scale_factor 在大表上是陷阱:它讓觸發
  點跟著表一起長,2000 萬列時就算設 0.02 也回到 40 萬,問題原封不動回來。固定值才能
  讓「落後多少列」與表大小脫鉤。

  調勤不會等比例變貴。vacuum 自己也用 VM 跳過乾淨的頁面,成本跟髒頁數成正比而不是
  表大小 —— 實測 40 萬筆髒頁 51ms、4 萬筆 19ms、完全乾淨的空跑 18ms。所以「10 倍
  頻率」換來的是「每次 1/2.6 的成本」,不是 10 倍總成本。

代價:訂單還不到兩萬筆的階段幾乎不會被 autovacuum 碰。那時候沒有東西值得清,而
analyze 的門檻設得低一些(1 萬)讓查詢計畫仍然跟得上。

Revision ID: c3e8b5f2091d
Revises: b64d2af9137e
Create Date: 2026-08-24 17:40:00.000000

"""
from typing import Sequence, Union

from alembic import op

from app.models.order import ORDERS_AUTOVACUUM


# revision identifiers, used by Alembic.
revision: str = 'c3e8b5f2091d'
down_revision: Union[str, Sequence[str], None] = 'b64d2af9137e'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    # ALTER TABLE ... SET (...) 只改 pg_class.reloptions,不重寫資料、不掃表。
    # 它拿的是 SHARE UPDATE EXCLUSIVE —— 不擋讀也不擋寫,只跟其他 DDL 與 vacuum 互斥。
    settings = ", ".join(f"{k} = {v}" for k, v in ORDERS_AUTOVACUUM.items())
    op.execute(f"ALTER TABLE orders SET ({settings})")


def downgrade() -> None:
    # RESET 回到 postgresql.conf / RDS parameter group 的全域值。
    names = ", ".join(ORDERS_AUTOVACUUM)
    op.execute(f"ALTER TABLE orders RESET ({names})")
