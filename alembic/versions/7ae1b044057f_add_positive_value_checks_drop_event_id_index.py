"""add positive-value checks on orders/events; drop redundant ix_orders_event_id

Revision ID: 7ae1b044057f
Revises: 7dc383f92999
Create Date: 2026-07-28 10:00:00.000000

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = '7ae1b044057f'
down_revision: Union[str, Sequence[str], None] = '7dc383f92999'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    # Value sanity — the DB is the last line for rows the worker INSERTs off a Redis
    # Stream payload (which bypasses the Pydantic request-layer validation), and for
    # any direct admin/backfill write.
    op.create_check_constraint("ck_orders_quantity_pos", "orders", "quantity > 0")
    op.create_check_constraint("ck_orders_total_nonneg", "orders", "total_price_cents >= 0")
    op.create_check_constraint("ck_events_total_seats_pos", "events", "total_seats > 0")
    op.create_check_constraint("ck_events_price_nonneg", "events", "price_cents >= 0")

    # Drop the standalone event_id index: pure write amplification on the hot INSERT
    # path. NOTE (corrected 2026-10-06): the rationale originally written here --
    # "ix_orders_active's leftmost prefix serves every event_id filter" -- was wrong.
    # ix_orders_active is PARTIAL (WHERE status IN held), so it only serves queries
    # that also carry that status predicate. The decision still holds because
    # (1) every app reader on event_id does carry it, and (2) the only path that
    # cannot use a partial index is the RI reverse lookup on DELETE of events /
    # event_zone_prices, which never runs. See the event_id comment in
    # app/models/order.py for the current reasoning.
    op.drop_index(op.f("ix_orders_event_id"), table_name="orders")


def downgrade() -> None:
    op.create_index(op.f("ix_orders_event_id"), "orders", ["event_id"], unique=False)
    op.drop_constraint("ck_events_price_nonneg", "events", type_="check")
    op.drop_constraint("ck_events_total_seats_pos", "events", type_="check")
    op.drop_constraint("ck_orders_total_nonneg", "orders", type_="check")
    op.drop_constraint("ck_orders_quantity_pos", "orders", type_="check")
