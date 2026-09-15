"""add outbox table

Transactional Outbox 的落地(設計討論見 app/models/outbox.py 的 docstring)。
schema 層只有一個非顯而易見的點:ix_outbox_unprocessed 是 partial
(WHERE processed_at IS NULL)—— relay 的工作集常態接近零,索引體積跟 backlog
走而不是跟歷史總量走,同 ix_orders_pending_sweep 的形狀。

Revision ID: b3d47f81c026
Revises: a9d02e75c318
Create Date: 2026-09-02 11:20:00.000000

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

# revision identifiers, used by Alembic.
revision: str = 'b3d47f81c026'
down_revision: Union[str, Sequence[str], None] = 'a9d02e75c318'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    """Upgrade schema."""
    op.create_table(
        'outbox',
        sa.Column('id', sa.BigInteger(), autoincrement=True, nullable=False),
        sa.Column('event_type', sa.String(length=64), nullable=False),
        sa.Column('aggregate_type', sa.String(length=32), nullable=False),
        sa.Column('aggregate_id', sa.BigInteger(), nullable=False),
        sa.Column('payload', postgresql.JSONB(astext_type=sa.Text()), nullable=False),
        sa.Column('attempts', sa.SmallInteger(), server_default=sa.text('0'), nullable=False),
        sa.Column('next_attempt_at', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=False),
        sa.Column('processed_at', sa.DateTime(timezone=True), nullable=True),
        sa.Column('created_at', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=False),
        sa.PrimaryKeyConstraint('id'),
    )
    op.create_index(
        'ix_outbox_unprocessed',
        'outbox',
        ['id'],
        unique=False,
        postgresql_where=sa.text('processed_at IS NULL'),
    )


def downgrade() -> None:
    """Downgrade schema."""
    op.drop_index('ix_outbox_unprocessed', table_name='outbox')
    op.drop_table('outbox')
