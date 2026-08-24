"""add partial index for the pending-order expiry sweep

Revision ID: 594fdbaa7d1c
Revises: 7ae1b044057f
Create Date: 2026-07-28 10:05:00.000000

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = '594fdbaa7d1c'
down_revision: Union[str, Sequence[str], None] = '7ae1b044057f'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    # Matches worker.expire_pending_orders' WHERE exactly (status='pending' AND
    # payment_provider_id IS NULL, range-scanned by created_at), turning a per-minute
    # seq scan of orders into a tiny index range scan. Stays small: rows leave the
    # index as they transition out of PENDING or gain a payment_provider_id.
    #
    # CREATE INDEX CONCURRENTLY cannot run inside a transaction; autocommit_block
    # temporarily leaves the migration's transaction so the build does NOT take an
    # ACCESS EXCLUSIVE lock and block the order-consumer's INSERTs during deploy.
    with op.get_context().autocommit_block():
        op.create_index(
            "ix_orders_pending_sweep",
            "orders",
            ["created_at"],
            postgresql_where=sa.text("status = 'pending' AND payment_provider_id IS NULL"),
            postgresql_concurrently=True,
            if_not_exists=True,
        )


def downgrade() -> None:
    with op.get_context().autocommit_block():
        op.drop_index(
            "ix_orders_pending_sweep",
            table_name="orders",
            postgresql_concurrently=True,
            if_exists=True,
        )
