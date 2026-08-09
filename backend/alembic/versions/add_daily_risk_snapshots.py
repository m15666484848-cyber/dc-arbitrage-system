"""add daily risk snapshots

Revision ID: add_daily_risk_snapshots
Revises: add_pending_condition_trigger
Create Date: 2026-08-08
"""
from alembic import op
import sqlalchemy as sa

revision = "add_daily_risk_snapshots"
down_revision = "add_pending_condition_trigger"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "daily_risk_snapshots",
        sa.Column("id", sa.Integer(), primary_key=True, autoincrement=True),
        sa.Column("customer_id", sa.Integer(), sa.ForeignKey("customers.id", ondelete="CASCADE"), nullable=False),
        sa.Column("exchange", sa.String(length=32), nullable=False, server_default="all"),
        sa.Column("day", sa.Date(), nullable=False),
        sa.Column("realized_pnl", sa.Float(), nullable=False, server_default="0"),
        sa.Column("unrealized_pnl", sa.Float(), nullable=False, server_default="0"),
        sa.Column("total_daily_pnl", sa.Float(), nullable=False, server_default="0"),
        sa.Column("equity", sa.Float(), nullable=False, server_default="0"),
        sa.Column("balance", sa.Float(), nullable=False, server_default="0"),
        sa.Column("base_equity", sa.Float(), nullable=False, server_default="0"),
        sa.Column("max_daily_loss_pct", sa.Float(), nullable=False, server_default="0"),
        sa.Column("loss_pct", sa.Float(), nullable=False, server_default="0"),
        sa.Column("risk_level", sa.String(length=16), nullable=False, server_default="normal"),
        sa.Column("risk_triggered", sa.Boolean(), nullable=False, server_default=sa.false()),
        sa.Column("open_positions", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("trade_count", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("close_count", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("snapshot_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
    )
    op.create_index("ix_daily_risk_snapshots_customer_id", "daily_risk_snapshots", ["customer_id"])
    op.create_index("ix_daily_risk_snapshots_exchange", "daily_risk_snapshots", ["exchange"])
    op.create_index("ix_daily_risk_snapshots_day", "daily_risk_snapshots", ["day"])
    op.create_index("ix_daily_risk_snapshots_snapshot_at", "daily_risk_snapshots", ["snapshot_at"])
    op.create_index("uq_daily_risk_customer_exchange_day", "daily_risk_snapshots", ["customer_id", "exchange", "day"], unique=True)


def downgrade() -> None:
    op.drop_table("daily_risk_snapshots")
