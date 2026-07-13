"""drop redundant ix_orders_user_id (covered by composite)

Revision ID: 76167cb2eb6f
Revises: 67365b08b304
Create Date: 2026-07-13 11:31:17.669821

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = '76167cb2eb6f'
down_revision: Union[str, Sequence[str], None] = '67365b08b304'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    """ix_orders_user_created (user_id, ...) covers user_id-prefix lookups, so the
    standalone single-column index is redundant write overhead."""
    op.drop_index("ix_orders_user_id", table_name="orders")


def downgrade() -> None:
    op.create_index("ix_orders_user_id", "orders", ["user_id"])
