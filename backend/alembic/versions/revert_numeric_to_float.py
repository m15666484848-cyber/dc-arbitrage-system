"""Revert tech_debt_migration M-8: Numeric(20,8) -> Float (double precision)

Revision ID: revert_numeric_to_float
Revises: tech_debt_fix
Create Date: 2026-08-14
"""
from alembic import op
import sqlalchemy as sa

revision = "revert_numeric_to_float"
down_revision = "tech_debt_fix"
branch_labels = None
depends_on = None

# All columns that were changed from Float to Numeric(20,8) by tech_debt_migration
# Reverting back to Float (double precision) to match model definitions
NUMERIC_COLUMNS = [
    ("customer_kol_configs", "follow_weight"),
    ("customer_kol_configs", "max_order_usdt"),
    ("risk_configs", "max_position_usdt"),
    ("risk_configs", "max_daily_loss_pct"),
    ("risk_configs", "per_kol_max_usdt"),
    ("risk_configs", "auto_stop_loss_pct"),
    ("risk_configs", "trailing_callback_pct"),
    ("daily_risk_snapshots", "equity"),
    ("daily_risk_snapshots", "balance"),
    ("daily_risk_snapshots", "unrealized_pnl"),
    ("daily_risk_snapshots", "realized_pnl"),
    ("daily_risk_snapshots", "total_daily_pnl"),
    ("daily_risk_snapshots", "base_equity"),
    ("daily_risk_snapshots", "max_daily_loss_pct"),
    ("daily_risk_snapshots", "loss_pct"),
    ("system_configs", "text_llm_temperature"),
    ("system_configs", "vision_llm_temperature"),
    ("customers", "max_order_usdt"),
    ("customer_multipliers", "multiplier"),
    ("kols", "llm_min_confidence"),
    ("kol_follows", "followed_notional_usdt"),
    ("pending_orders", "entry_price"),
    ("pending_orders", "condition_price"),
    ("pending_orders", "notional_usdt"),
    ("pending_orders", "sl"),
    ("referrals", "invitee_pnl"),
    ("referrals", "commission_rate"),
    ("referrals", "commission_amount"),
    ("signals", "confidence"),
    ("signals", "entry_price"),
    ("signals", "old_entry_price"),
    ("signals", "new_entry_price"),
    ("signals", "old_stop_loss"),
    ("signals", "new_stop_loss"),
    ("strategies", "last_qty"),
    ("symbol_configs", "multiplier"),
    ("orders", "notional_usdt"),
    ("orders", "qty"),
    ("orders", "price"),
    ("orders", "filled_qty"),
    ("orders", "filled_price"),
    ("positions", "entry_price"),
    ("positions", "qty"),
    ("positions", "initial_qty"),
    ("positions", "sl"),
    ("positions", "initial_sl"),
    ("positions", "trailing_callback"),
    ("positions", "realized_pnl"),
    ("positions", "entry_fee"),
    ("close_records", "qty"),
    ("close_records", "price"),
    ("close_records", "fee"),
    ("close_records", "realized_pnl"),
    ("equity_snapshots", "unrealized_pnl"),
    ("trades", "fee"),
    ("trades", "realized_pnl"),
]


def upgrade():
    for table, column in NUMERIC_COLUMNS:
        try:
            op.alter_column(table, column,
                type_=sa.Float(),
                existing_type=sa.Numeric(20, 8),
                postgresql_using=f"{column}::double precision",
            )
            print(f"OK: {table}.{column} -> Float")
        except Exception as e:
            print(f"SKIP {table}.{column}: {e}")


def downgrade():
    for table, column in NUMERIC_COLUMNS:
        try:
            op.alter_column(table, column, type_=sa.Numeric(20, 8), existing_type=sa.Float())
        except Exception:
            pass
