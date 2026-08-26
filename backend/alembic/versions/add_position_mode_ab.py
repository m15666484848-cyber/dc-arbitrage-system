"""M-AB: exchange_accounts 加 position_mode/position_pct 下单模式

A+B方案: fixed=固定金额(策略基准×倍率,现状) | equity_pct=资金比例(账户权益×百分比)

Revision ID: add_position_mode_ab
Revises: tech_debt_fix
Create Date: 2026-08-26
"""
from alembic import op
import sqlalchemy as sa

revision = "add_position_mode_ab"
down_revision = "add_equity_snapshot_unique"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "exchange_accounts",
        sa.Column("position_mode", sa.String(16), nullable=False, server_default="fixed"),
    )
    op.add_column(
        "exchange_accounts",
        sa.Column("position_pct", sa.Float(), nullable=False, server_default="0"),
    )


def downgrade() -> None:
    op.drop_column("exchange_accounts", "position_pct")
    op.drop_column("exchange_accounts", "position_mode")
