"""add check constraints on orders status timestamps

Revision ID: a6528a2656ba
Revises: 35846ecd3442
Create Date: 2026-07-09 13:52:42.396930

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = 'a6528a2656ba'
down_revision: Union[str, Sequence[str], None] = '35846ecd3442'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    """Each milestone status must have its matching timestamp populated."""
    op.create_check_constraint(
        "ck_orders_paid_at", "orders",
        "status <> 'paid' OR paid_at IS NOT NULL",
    )
    op.create_check_constraint(
        "ck_orders_confirmed_at", "orders",
        "status <> 'confirmed' OR confirmed_at IS NOT NULL",
    )
    op.create_check_constraint(
        "ck_orders_expired_at", "orders",
        "status <> 'expired' OR expired_at IS NOT NULL",
    )
    op.create_check_constraint(
        "ck_orders_cancelled_at", "orders",
        "status <> 'cancelled' OR cancelled_at IS NOT NULL",
    )


def downgrade() -> None:
    """Downgrade schema."""
    op.drop_constraint("ck_orders_cancelled_at", "orders", type_="check")
    op.drop_constraint("ck_orders_expired_at", "orders", type_="check")
    op.drop_constraint("ck_orders_confirmed_at", "orders", type_="check")
    op.drop_constraint("ck_orders_paid_at", "orders", type_="check")
