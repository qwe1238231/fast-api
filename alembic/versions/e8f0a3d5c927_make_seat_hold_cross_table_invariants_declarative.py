"""make the seat-hold cross-table invariants enforceable

兩個原本 DB 容得下、但系統到不了(今天)的狀態,一次封掉:

1. **hold 的 block 屬於別的場館**(審查清單 #2)。event_id 與 block_id 各自的外鍵
   獨立成立,中間「同一個場館」那條沒人看。把 zone_id 冗餘進 seat_holds,兩條複合
   外鍵純宣告地表達:

       fk_seat_holds_zone_block   (zone_id, block_id) → seat_blocks (zone_id, id)
       fk_seat_holds_event_zone   (event_id, zone_id) → event_zone_prices 主鍵

   後者同時保證「同場館」(event_zone_prices 只收該場館的 zone,create_event 驗過)
   與「這一區有定價」。zone_id 自己不會漂移 —— 第一條外鍵恰好鎖住它必須是 block
   真正的 zone。

2. **hold 跟它的訂單不一致**(#3):場次不同、zone 不同、或座位數 != 張數。worker
   建 hold 的註解自己招認過這個隱含耦合(「若哪天出現買 2 送 1,這裡會靜默錯掉」)。
   用 trigger 而不是複合外鍵:宣告式的版本要 orders 上一個實測 60 MB 的唯一索引,
   而且每筆下單都要維護;trigger 把成本搬到「建 hold」那一側,用 orders_pkey 查一筆,
   零儲存。ERRCODE 23514 讓它以 IntegrityError 浮出,worker 既有的 dead-letter
   分支直接接手。

Revision ID: e8f0a3d5c927
Revises: d7a419c6b8e2
Create Date: 2026-08-24 21:30:00.000000

"""
from typing import Sequence, Union

from alembic import op

from app.models.seating import (
    SEAT_HOLD_MATCH_FUNCTION_SQL,
    SEAT_HOLD_MATCH_TRIGGER_SQL,
)


# revision identifiers, used by Alembic.
revision: str = 'e8f0a3d5c927'
down_revision: Union[str, Sequence[str], None] = 'd7a419c6b8e2'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None

BACKWARD_INCOMPATIBLE = """\
seat_holds 加了 NOT NULL 的 zone_id。滾動部署期間,舊 worker 建 hold 不帶這一欄,
INSERT 會被 NOT NULL 擋下。

判定可接受,而且是被 async-offload 的設計吸收掉的:order 與 hold 在同一個交易裡,
整個 txn 回滾,intent 不被 ack、留在 Redis Stream —— 幾分鐘內由(屆時已是新版的)
worker reclaim 重放成功。使用者看到的是訂單晚幾分鐘落帳,不是掉單。窗口是 ECS
滾動更換 worker 的那一兩分鐘。\
"""


def upgrade() -> None:
    # 1. 加欄位(nullable,瞬間)→ 回填 → 收 NOT NULL。
    #    回填的來源是 seat_blocks:block 知道自己的 zone,而既有 hold 的 block_id
    #    都是有效外鍵。
    op.execute("ALTER TABLE seat_holds ADD COLUMN zone_id INTEGER")
    op.execute(
        "UPDATE seat_holds SET zone_id = seat_blocks.zone_id "
        "FROM seat_blocks WHERE seat_blocks.id = seat_holds.block_id"
    )
    # SET NOT NULL 要全表掃並持有 ACCESS EXCLUSIVE —— 但 PG 12+ 看得懂已驗證的
    # CHECK:先用 NOT VALID + VALIDATE(只拿 SHARE UPDATE EXCLUSIVE)把「非 NULL」
    # 證明給它,SET NOT NULL 就變成純目錄操作,不再掃表。
    op.execute(
        "ALTER TABLE seat_holds ADD CONSTRAINT ck_seat_holds_zone_notnull "
        "CHECK (zone_id IS NOT NULL) NOT VALID"
    )
    op.execute("ALTER TABLE seat_holds VALIDATE CONSTRAINT ck_seat_holds_zone_notnull")
    op.execute("ALTER TABLE seat_holds ALTER COLUMN zone_id SET NOT NULL")
    op.execute("ALTER TABLE seat_holds DROP CONSTRAINT ck_seat_holds_zone_notnull")

    # 2. 外鍵的目標索引。seat_blocks 是場館幾何(幾百列),一般寫法即可。
    op.create_unique_constraint("uq_seat_blocks_zone_id", "seat_blocks", ["zone_id", "id"])

    # 3. 兩條複合外鍵。seat_holds 在下單路徑上,照例 NOT VALID + VALIDATE。
    for name, columns, target in (
        ("fk_seat_holds_zone_block", "(zone_id, block_id)", "seat_blocks (zone_id, id)"),
        (
            "fk_seat_holds_event_zone",
            "(event_id, zone_id)",
            "event_zone_prices (event_id, zone_id)",
        ),
    ):
        op.execute(
            f"ALTER TABLE seat_holds ADD CONSTRAINT {name} "
            f"FOREIGN KEY {columns} REFERENCES {target} NOT VALID"
        )
        op.execute(f"ALTER TABLE seat_holds VALIDATE CONSTRAINT {name}")

    # 4. trigger。定義 import 自 model(單一來源),跟 ORDERS_AUTOVACUUM 同一個慣例。
    op.execute(SEAT_HOLD_MATCH_FUNCTION_SQL)
    op.execute(SEAT_HOLD_MATCH_TRIGGER_SQL)


def downgrade() -> None:
    op.execute("DROP TRIGGER IF EXISTS trg_seat_holds_match_order ON seat_holds")
    op.execute("DROP FUNCTION IF EXISTS seat_holds_match_order()")
    op.drop_constraint("fk_seat_holds_event_zone", "seat_holds", type_="foreignkey")
    op.drop_constraint("fk_seat_holds_zone_block", "seat_holds", type_="foreignkey")
    op.drop_constraint("uq_seat_blocks_zone_id", "seat_blocks", type_="unique")
    op.drop_column("seat_holds", "zone_id")
