"""add seat map (venues/zones/blocks/seats), zone prices and seat_holds

純加法的 migration:既有的 events.venue 字串、events.total_seats、events.price_cents
都保留不動,新欄位 events.venue_id 與 orders.zone_id 都是 nullable。所以沒有座位圖
的場次照舊走純計數器的庫存路徑,座位功能只對 venue_id 有值的場次生效。等座位流程
完整上線、舊資料回填完,才另開一版把 venue 字串下架。

Revision ID: c41f8a2d5b70
Revises: 594fdbaa7d1c
Create Date: 2026-08-03
"""
from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = "c41f8a2d5b70"
down_revision: Union[str, Sequence[str], None] = "594fdbaa7d1c"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    # seat_holds 的 EXCLUDE 用 gist 同時比對整數相等與範圍重疊;
    # 「integer WITH =」在 gist 下需要 btree_gist。
    op.execute("CREATE EXTENSION IF NOT EXISTS btree_gist")

    op.create_table(
        "venues",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("name", sa.String(length=255), nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("name"),
    )

    op.create_table(
        "zones",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("venue_id", sa.Integer(), nullable=False),
        sa.Column("name", sa.String(length=64), nullable=False),
        sa.Column("display_order", sa.Integer(), nullable=False),
        sa.ForeignKeyConstraint(["venue_id"], ["venues.id"]),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("venue_id", "name", name="uq_zones_venue_name"),
    )
    op.create_index("ix_zones_venue_order", "zones", ["venue_id", "display_order"])

    op.create_table(
        "event_zone_prices",
        sa.Column("event_id", sa.Integer(), nullable=False),
        sa.Column("zone_id", sa.Integer(), nullable=False),
        sa.Column("price_cents", sa.Integer(), nullable=False),
        sa.CheckConstraint("price_cents >= 0", name="ck_event_zone_prices_nonneg"),
        sa.ForeignKeyConstraint(["event_id"], ["events.id"]),
        sa.ForeignKeyConstraint(["zone_id"], ["zones.id"]),
        sa.PrimaryKeyConstraint("event_id", "zone_id"),
    )

    op.create_table(
        "seat_blocks",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("zone_id", sa.Integer(), nullable=False),
        sa.Column("row_label", sa.String(length=8), nullable=False),
        sa.Column("block_index", sa.Integer(), nullable=False),
        sa.Column("capacity", sa.Integer(), nullable=False),
        sa.Column("quality_base", sa.Float(), nullable=False),
        sa.Column("quality_edge", sa.Float(), nullable=False),
        sa.CheckConstraint("capacity > 0", name="ck_seat_blocks_capacity_pos"),
        sa.CheckConstraint(
            "quality_edge >= 0 AND quality_edge <= quality_base AND quality_base <= 1",
            name="ck_seat_blocks_quality_range",
        ),
        sa.ForeignKeyConstraint(["zone_id"], ["zones.id"]),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint(
            "zone_id", "row_label", "block_index", name="uq_seat_blocks_pos"
        ),
    )

    op.create_table(
        "seats",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("block_id", sa.Integer(), nullable=False),
        sa.Column("pos", sa.Integer(), nullable=False),
        sa.Column("label", sa.String(length=16), nullable=False),
        sa.CheckConstraint("pos >= 0", name="ck_seats_pos_nonneg"),
        sa.ForeignKeyConstraint(["block_id"], ["seat_blocks.id"]),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("block_id", "pos", name="uq_seats_block_pos"),
        sa.UniqueConstraint("block_id", "label", name="uq_seats_block_label"),
    )

    op.create_table(
        "seat_holds",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("event_id", sa.Integer(), nullable=False),
        sa.Column("block_id", sa.Integer(), nullable=False),
        sa.Column("order_id", sa.Integer(), nullable=False),
        sa.Column("start_pos", sa.Integer(), nullable=False),
        sa.Column("length", sa.Integer(), nullable=False),
        # 生成欄位,唯一用途是給下面那條複合外鍵指。
        sa.Column(
            "last_pos",
            sa.Integer(),
            sa.Computed("start_pos + length - 1", persisted=True),
            nullable=False,
        ),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.Column("confirmed_at", sa.DateTime(timezone=True), nullable=True),
        sa.CheckConstraint("length > 0", name="ck_seat_holds_length_pos"),
        sa.CheckConstraint("start_pos >= 0", name="ck_seat_holds_start_nonneg"),
        sa.ForeignKeyConstraint(["event_id"], ["events.id"]),
        sa.ForeignKeyConstraint(["block_id"], ["seat_blocks.id"]),
        sa.ForeignKeyConstraint(["order_id"], ["orders.id"]),
        # 區間不得超出 block 容量 —— 跨表條件,CHECK 表達不了,但「生成欄位 +
        # 指向 seats(block_id, pos) 的複合外鍵」可以:最後一個 pos 必須真的存在。
        sa.ForeignKeyConstraint(
            ["block_id", "last_pos"],
            ["seats.block_id", "seats.pos"],
            name="fk_seat_holds_last_seat",
        ),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("order_id", name="uq_seat_holds_order"),
    )
    # 「兩個人拿到同一張椅子」在資料庫層面物理上不可能。DEFERRABLE 是留給
    # compaction 的:整批滑動 hold 時中間狀態會短暫重疊,那個 txn 裡
    # SET CONSTRAINTS ALL DEFERRED 就能做任意排列。
    op.execute(
        """
        ALTER TABLE seat_holds ADD CONSTRAINT ex_seat_holds_no_overlap
        EXCLUDE USING gist (
            event_id WITH =,
            block_id WITH =,
            int4range(start_pos, start_pos + length) WITH &&
        ) DEFERRABLE INITIALLY IMMEDIATE
        """
    )

    op.add_column("events", sa.Column("venue_id", sa.Integer(), nullable=True))
    op.create_foreign_key(
        "fk_events_venue_id", "events", "venues", ["venue_id"], ["id"]
    )
    op.create_index("ix_events_venue_id", "events", ["venue_id"])

    op.add_column("orders", sa.Column("zone_id", sa.Integer(), nullable=True))
    op.create_foreign_key("fk_orders_zone_id", "orders", "zones", ["zone_id"], ["id"])


def downgrade() -> None:
    op.drop_constraint("fk_orders_zone_id", "orders", type_="foreignkey")
    op.drop_column("orders", "zone_id")

    op.drop_index("ix_events_venue_id", table_name="events")
    op.drop_constraint("fk_events_venue_id", "events", type_="foreignkey")
    op.drop_column("events", "venue_id")

    op.drop_table("seat_holds")
    op.drop_table("seats")
    op.drop_table("seat_blocks")
    op.drop_table("event_zone_prices")
    op.drop_index("ix_zones_venue_order", table_name="zones")
    op.drop_table("zones")
    op.drop_table("venues")
    # btree_gist 刻意不 DROP:別的東西可能也在用它,而且留著沒有成本。
