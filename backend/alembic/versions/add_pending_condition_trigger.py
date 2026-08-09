"""add condition trigger fields to pending orders

Revision ID: add_pending_condition_trigger
Revises: add_symbol_notional_config
Create Date: 2026-08-08
"""
from alembic import op
import sqlalchemy as sa

revision = "add_pending_condition_trigger"
down_revision = "add_symbol_notional_config"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column("pending_orders", sa.Column("condition_price", sa.Float(), nullable=True))
    op.add_column("pending_orders", sa.Column("condition_direction", sa.String(length=16), nullable=False, server_default=""))
    op.add_column("pending_orders", sa.Column("trigger_mode", sa.String(length=32), nullable=False, server_default="entry"))
    op.add_column("pending_orders", sa.Column("condition_triggered_at", sa.DateTime(timezone=True), nullable=True))


def downgrade() -> None:
    op.drop_column("pending_orders", "condition_triggered_at")
    op.drop_column("pending_orders", "trigger_mode")
    op.drop_column("pending_orders", "condition_direction")
    op.drop_column("pending_orders", "condition_price")
