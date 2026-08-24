"""three decided cleanups: price source, buyer_info PK, users.created_at

2026-08-24 拍板的三條(#9/#11/#12;#10 venue 字串宣告為顯示欄位,純註解不動 schema):

#9  events.price_cents 改 nullable + 互斥 CHECK
      (venue_id IS NULL) = (price_cents IS NOT NULL)
    以前座位場次塞哨兵值 0,而 0 剛好通過 ck_events_price_nonneg ——「忘記設價」、
    「真的免費場」、「座位場次」三者在 DB 裡長一樣。NULL 讓價格來源變成 schema
    說得出口的事。既有座位場次的 0 回填成 NULL。

#11 buyer_info 的主鍵從 surrogate id 換成 user_id
    嚴格 1:1(原本就有 unique(user_id)),surrogate id 是多一個序列跟索引,而全
    repo 沒有任何地方引用它。連帶 uq(user_id) 也不需要了 —— PK 自帶唯一性。

#12 users.created_at(timestamptz NOT NULL server_default now())
    既有帳號回填為 migration 當下時間:真實建立時間已失傳,這是誠實的近似 ——
    NULL 會讓每個讀取端都要處理特例,更糟。PG 11+ 的 fast default,不重寫表。

Revision ID: a9d02e75c318
Revises: f2b6c81d4a05
Create Date: 2026-08-24 23:55:00.000000

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = 'a9d02e75c318'
down_revision: Union[str, Sequence[str], None] = 'f2b6c81d4a05'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None

BACKWARD_INCOMPATIBLE = """\
兩處會被守門掃描標出來,各自的滾動部署論證:

ADD CHECK(ck_events_price_source):舊 task 建座位場次時寫 price_cents=0 →
違反新 CHECK → 建立失敗。這是管理員操作(低頻、可重試),滾動窗口一兩分鐘,
失敗訊息明確;付費路徑完全不受影響(下單不寫 events)。

DROP COLUMN(buyer_info.id):舊 task 的 ORM 對 buyer_info 的 SELECT 仍會列出
id 欄位 → 讀取路徑在窗口內失敗。buyer_info 只有兩條低頻端點(POST /buyer-info、
GET /buyer-info/me),失敗是明確的 500 而非靜默錯誤,窗口過後自癒。搶票與付款
路徑不碰這張表。\
"""


def upgrade() -> None:
    # ── #9 price source ──
    op.execute("ALTER TABLE events ALTER COLUMN price_cents DROP NOT NULL")
    # 回填在加 CHECK 之前:座位場次的哨兵 0 → NULL。
    op.execute("UPDATE events SET price_cents = NULL WHERE venue_id IS NOT NULL")
    # events 是小表(幾百列、整份被快取),一般 ADD CHECK 即可 —— 跟 b7e2c94a 同一個取捨。
    op.create_check_constraint(
        "ck_events_price_source", "events",
        "(venue_id IS NULL) = (price_cents IS NOT NULL)",
    )

    # ── #11 buyer_info PK ──
    # 順序:先給新 PK、再拆舊的。buyer_info 是小表,ACCESS EXCLUSIVE 是毫秒級。
    op.drop_constraint("buyer_info_user_id_key", "buyer_info", type_="unique")
    op.drop_constraint("buyer_info_pkey", "buyer_info", type_="primary")
    op.create_primary_key("buyer_info_pkey", "buyer_info", ["user_id"])
    # DROP COLUMN 連帶帶走它擁有的 sequence(OWNED BY)。
    op.drop_column("buyer_info", "id")

    # ── #12 users.created_at ──
    # PG 11+ 的 fast default:server_default 讓既有列拿到 migration 當下時間,
    # 不重寫表、不掃描 —— add_column + server_default 正是守門掃描豁免的形狀。
    op.add_column(
        "users",
        sa.Column(
            "created_at", sa.DateTime(timezone=True),
            server_default=sa.text("now()"), nullable=False,
        ),
    )


def downgrade() -> None:
    op.drop_column("users", "created_at")

    op.add_column(
        "buyer_info",
        sa.Column("id", sa.BigInteger(), sa.Identity(), nullable=False),
    )
    op.drop_constraint("buyer_info_pkey", "buyer_info", type_="primary")
    op.create_primary_key("buyer_info_pkey", "buyer_info", ["id"])
    op.create_unique_constraint("buyer_info_user_id_key", "buyer_info", ["user_id"])

    op.drop_constraint("ck_events_price_source", "events", type_="check")
    op.execute("UPDATE events SET price_cents = 0 WHERE price_cents IS NULL")
    op.execute("ALTER TABLE events ALTER COLUMN price_cents SET NOT NULL")
