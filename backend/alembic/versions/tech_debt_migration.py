"""技术债务修复: M-8 Float→Numeric, M-10 partial index, L-5 ocr_text, L-1 apscheduler_jobs

Revision ID: tech_debt_fix
Revises: add_martingale_state
Create Date: 2026-08-13
"""
from alembic import op
import sqlalchemy as sa

revision = "tech_debt_fix"
down_revision = "add_martingale_state"
branch_labels = None
depends_on = None

# M-8: 所有 Float → Numeric(20, 8) 的表和列
FLOAT_COLUMNS = [
    # config.py — CustomerKolConfig
    ("customer_kol_configs", "follow_weight"),
    ("customer_kol_configs", "max_order_usdt"),
    # config.py — RiskConfig
    ("risk_configs", "max_position_usdt"),
    ("risk_configs", "max_daily_loss_pct"),
    ("risk_configs", "per_kol_max_usdt"),
    ("risk_configs", "auto_stop_loss_pct"),
    ("risk_configs", "trailing_callback_pct"),
    # config.py — DailyRiskSnapshot
    ("daily_risk_snapshots", "equity"),
    ("daily_risk_snapshots", "balance"),
    ("daily_risk_snapshots", "unrealized_pnl"),
    ("daily_risk_snapshots", "realized_pnl"),
    ("daily_risk_snapshots", "total_daily_pnl"),
    ("daily_risk_snapshots", "base_equity"),
    ("daily_risk_snapshots", "max_daily_loss_pct"),
    ("daily_risk_snapshots", "loss_pct"),
    # config.py — SystemConfig
    ("system_configs", "text_llm_temperature"),
    ("system_configs", "vision_llm_temperature"),
    # customer.py
    ("customers", "max_order_usdt"),
    # customer_multiplier.py
    ("customer_multipliers", "multiplier"),
    # kol.py
    ("kols", "llm_min_confidence"),
    ("kol_follows", "followed_notional_usdt"),
    # pending_order.py
    ("pending_orders", "entry_price"),
    ("pending_orders", "condition_price"),
    ("pending_orders", "notional_usdt"),
    ("pending_orders", "sl"),
    # referral.py
    ("referrals", "invitee_pnl"),
    ("referrals", "commission_rate"),
    ("referrals", "commission_amount"),
    # signal.py
    ("signals", "confidence"),
    ("signals", "entry_price"),
    ("signals", "old_entry_price"),
    ("signals", "new_entry_price"),
    ("signals", "old_stop_loss"),
    ("signals", "new_stop_loss"),
    # strategy.py
    ("strategies", "last_qty"),
    # symbol_config.py
    ("symbol_configs", "multiplier"),
    # trading.py — Order
    ("orders", "notional_usdt"),
    ("orders", "qty"),
    ("orders", "price"),
    ("orders", "filled_qty"),
    ("orders", "filled_price"),
    # trading.py — Position
    ("positions", "entry_price"),
    ("positions", "qty"),
    ("positions", "initial_qty"),
    ("positions", "sl"),
    ("positions", "initial_sl"),
    ("positions", "trailing_callback"),
    ("positions", "realized_pnl"),
    ("positions", "entry_fee"),
    # trading.py — CloseRecord
    ("close_records", "qty"),
    ("close_records", "price"),
    ("close_records", "fee"),
    ("close_records", "realized_pnl"),
]


def upgrade():
    # === M-8: Float → Numeric(20, 8) ===
    for table, column in FLOAT_COLUMNS:
        try:
            op.alter_column(table, column,
                type_=sa.Numeric(20, 8),
                existing_type=sa.Float(),
                postgresql_using=f"{column}::numeric(20,8)",
            )
        except Exception as e:
            print(f"SKIP M-8 {table}.{column}: {e}")

    # === L-5: 添加 ocr_text 列到 signals 表 ===
    try:
        op.add_column("signals", sa.Column("ocr_text", sa.Text(), server_default=""))
    except Exception as e:
        print(f"SKIP L-5 signals.ocr_text: {e}")

    # === M-10: 添加 partial unique index (排除 NULL) ===
    # customer_multipliers 的 custom_symbol 可能为 NULL,PostgreSQL NULL 不参与唯一约束
    try:
        op.create_index(
            "uq_customer_symbol_not_null",
            "customer_multipliers",
            ["customer_id", "custom_symbol"],
            unique=True,
            postgresql_where=sa.text("custom_symbol IS NOT NULL"),
        )
    except Exception as e:
        print(f"SKIP M-10 partial index: {e}")

    # === L-1: 创建 apscheduler_jobs 表 ===
    try:
        op.create_table(
            "apscheduler_jobs",
            sa.Column("id", sa.String(191), primary_key=True),
            sa.Column("next_run_time", sa.DateTime(timezone=True), index=True),
            sa.Column("job_state", sa.LargeBinary(), nullable=False),
            sa.Column("func_name", sa.String(512)),
        )
    except Exception as e:
        print(f"SKIP L-1 apscheduler_jobs: {e}")


def downgrade():
    # Revert M-8
    for table, column in FLOAT_COLUMNS:
        try:
            op.alter_column(table, column, type_=sa.Float(), existing_type=sa.Numeric(20, 8))
        except Exception:
            pass

    # Revert L-5
    try:
        op.drop_column("signals", "ocr_text")
    except Exception:
        pass

    # Revert M-10
    try:
        op.drop_index("uq_customer_symbol_not_null", table_name="customer_multipliers")
    except Exception:
        pass

    # Revert L-1
    try:
        op.drop_table("apscheduler_jobs")
    except Exception:
        pass
