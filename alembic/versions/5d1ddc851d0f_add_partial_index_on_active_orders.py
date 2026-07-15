"""add partial index on active orders

Revision ID: 5d1ddc851d0f
Revises: 2914167a8474
Create Date: 2026-07-01 14:08:54.468789

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = '5d1ddc851d0f'
down_revision: Union[str, Sequence[str], None] = '2914167a8474'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_index(
        "ix_orders_active",
        "orders",
        ["event_id", "status"],
        postgresql_where=sa.text("status IN ('pending', 'paid', 'confirmed')"),
    )

def downgrade() -> None:
    op.drop_index("ix_orders_active", table_name="orders")