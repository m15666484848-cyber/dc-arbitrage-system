"""添加马丁策略按 KOL 和币种隔离的状态字段

Revision ID: add_martingale_state
Revises: merge_daily_risk_discord_heads
Create Date: 2026-08-10
"""
from typing import Sequence, Union

from alembic import op


revision: str = "add_martingale_state"
down_revision: Union[str, Sequence[str], None] = "merge_daily_risk_discord_heads"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    # 线上库此前可能已由启动期手动迁移加过该字段；这里保持幂等，避免 Alembic 接管时重复添加失败。
    op.execute(
        "ALTER TABLE strategies "
        "ADD COLUMN IF NOT EXISTS martingale_state JSONB NOT NULL DEFAULT '{}'::jsonb"
    )


def downgrade() -> None:
    op.execute("ALTER TABLE strategies DROP COLUMN IF EXISTS martingale_state")
