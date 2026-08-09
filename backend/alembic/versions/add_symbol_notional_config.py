"""添加品种分类倍率配置表

Revision ID: add_symbol_notional_config
Revises: add_followed_notional
Create Date: 2026-08-05 14:00:00.000000
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


revision: str = 'add_symbol_notional_config'
down_revision: Union[str, None] = 'add_followed_notional'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table(
        'symbol_notional_configs',
        sa.Column('id', sa.Integer(), autoincrement=True, nullable=False),
        sa.Column('name', sa.String(length=50), nullable=False),
        sa.Column('symbols', sa.String(length=500), nullable=False, server_default=''),
        sa.Column('multiplier', sa.Float(), nullable=False, server_default='1.0'),
        sa.Column('enabled', sa.Boolean(), nullable=False, server_default=sa.text('true')),
        sa.Column('note', sa.String(length=200), nullable=True), server_default=''),
        sa.Column('created_at', sa.DateTime(), server_default=sa.func.now(), nullable=False),
        sa.Column('updated_at', sa.DateTime(), server_default=sa.func.now(), onupdate=sa.func.now(), nullable=False),
        sa.PrimaryKeyConstraint('id'),
        sa.UniqueConstraint('name'),
    )

    op.create_index(op.fix('ix_symbol_notional_configs_id'), 'symbol_notional_configs', ['id'])


def downgrade() -> None:
    op.drop_index(op.fix('ix_symbol_notional_configs_id'), table_name='symbol_notional_configs')
    op.drop_table('symbol_notional_configs')
