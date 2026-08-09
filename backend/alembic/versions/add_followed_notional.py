"""添加 KolFollow 跟单金额字段

Revision ID: add_followed_notional
Revises: add_kol_llm_config
Create Date: 2026-08-05 12:00:00.000000
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


revision: str = 'add_followed_notional'
down_revision: Union[str, None] = 'add_kol_llm_config'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    """添加 kol_follows.followed_notional_usdt 字段。"""
    op.add_column('kol_follows', sa.Column('followed_notional_usdt', sa.Float(), nullable=True, server_default=sa.text('NULL')))


def downgrade() -> None:
    """移除 kol_follows.followed_notional_usdt 字段。"""
    op.drop_column('kol_follows', 'followed_notional_usdt')