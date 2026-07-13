"""add keyset index on orders user_id, created_at, id

Revision ID: 67365b08b304
Revises: a6528a2656ba
Create Date: 2026-07-13 10:43:40.201667

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = '67365b08b304'
down_revision: Union[str, Sequence[str], None] = 'a6528a2656ba'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    """Composite index for keyset pagination of a user's orders."""
    op.create_index(
        "ix_orders_user_created",
        "orders",
        ["user_id", "created_at", "id"],
    )


def downgrade() -> None:
    op.drop_index("ix_orders_user_created", table_name="orders")
