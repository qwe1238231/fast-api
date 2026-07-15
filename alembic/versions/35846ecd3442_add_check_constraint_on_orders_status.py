"""add check constraint on orders status

Revision ID: 35846ecd3442
Revises: 5d1ddc851d0f
Create Date: 2026-07-01 15:27:23.291019

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = '35846ecd3442'
down_revision: Union[str, Sequence[str], None] = '5d1ddc851d0f'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_check_constraint(
        "ck_orders_status",
        "orders",
        "status IN ('pending', 'paid', 'confirmed', 'expired', 'cancelled')",
    )

def downgrade() -> None:
    op.drop_constraint("ck_orders_status", "orders", type_="check")
