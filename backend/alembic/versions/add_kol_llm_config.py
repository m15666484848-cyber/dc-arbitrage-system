"""添加 KOL LLM 配置字段

Revision ID: add_kol_llm_config
Revises: 
Create Date: 2026-08-03 12:00:00.000000
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = 'add_kol_llm_config'
down_revision: Union[str, None] = None
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    """添加 KOL LLM 配置字段。"""
    op.add_column('kols', sa.Column('llm_enabled', sa.Boolean(), server_default='false', nullable=False))
    op.add_column('kols', sa.Column('llm_image_analysis', sa.Boolean(), server_default='false', nullable=False))
    op.add_column('kols', sa.Column('llm_fallback', sa.Boolean(), server_default='true', nullable=False))
    op.add_column('kols', sa.Column('llm_min_confidence', sa.Float(), server_default='0.4', nullable=False))
    op.add_column('kols', sa.Column('llm_provider', sa.String(length=32), server_default='', nullable=False))
    op.add_column('kols', sa.Column('llm_model', sa.String(length=64), server_default='', nullable=False))
    op.add_column('kols', sa.Column('llm_calls_total', sa.Integer(), server_default='0', nullable=False))
    op.add_column('kols', sa.Column('llm_calls_success', sa.Integer(), server_default='0', nullable=False))
    op.add_column('kols', sa.Column('llm_tokens_used', sa.Integer(), server_default='0', nullable=False))


def downgrade() -> None:
    """移除 KOL LLM 配置字段。"""
    op.drop_column('kols', 'llm_tokens_used')
    op.drop_column('kols', 'llm_calls_success')
    op.drop_column('kols', 'llm_calls_total')
    op.drop_column('kols', 'llm_model')
    op.drop_column('kols', 'llm_provider')
    op.drop_column('kols', 'llm_min_confidence')
    op.drop_column('kols', 'llm_fallback')
    op.drop_column('kols', 'llm_image_analysis')
    op.drop_column('kols', 'llm_enabled')