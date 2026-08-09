# Consolidate schema changes previously maintained in app startup.
#
# Revision ID: consolidate_startup_schema
# Revises: merge_daily_risk_discord_heads
# Create Date: 2026-08-09
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


revision: str = "consolidate_startup_schema"
down_revision: Union[str, Sequence[str], None] = "merge_daily_risk_discord_heads"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    # Idempotent consolidation of legacy startup DDL. Keep future schema changes here, not in app/main.py.
    statements = [
        "ALTER TABLE system_config ADD COLUMN IF NOT EXISTS text_llm_provider VARCHAR(32) DEFAULT 'deepseek'",
        "ALTER TABLE system_config ADD COLUMN IF NOT EXISTS text_llm_api_key_enc TEXT DEFAULT ''",
        "ALTER TABLE system_config ADD COLUMN IF NOT EXISTS text_llm_model VARCHAR(64) DEFAULT ''",
        "ALTER TABLE system_config ADD COLUMN IF NOT EXISTS text_llm_api_base VARCHAR(256) DEFAULT ''",
        "ALTER TABLE system_config ADD COLUMN IF NOT EXISTS text_llm_temperature FLOAT DEFAULT 0.1",
        "ALTER TABLE system_config ADD COLUMN IF NOT EXISTS text_llm_max_tokens INTEGER DEFAULT 2000",
        "ALTER TABLE system_config ADD COLUMN IF NOT EXISTS text_llm_timeout INTEGER DEFAULT 30",
        "ALTER TABLE system_config ADD COLUMN IF NOT EXISTS vision_llm_enabled BOOLEAN DEFAULT FALSE",
        "ALTER TABLE system_config ADD COLUMN IF NOT EXISTS vision_llm_provider VARCHAR(32) DEFAULT 'zhipu'",
        "ALTER TABLE system_config ADD COLUMN IF NOT EXISTS vision_llm_api_key_enc TEXT DEFAULT ''",
        "ALTER TABLE system_config ADD COLUMN IF NOT EXISTS vision_llm_model VARCHAR(64) DEFAULT ''",
        "ALTER TABLE system_config ADD COLUMN IF NOT EXISTS vision_llm_api_base VARCHAR(256) DEFAULT ''",
        "ALTER TABLE system_config ADD COLUMN IF NOT EXISTS vision_llm_temperature FLOAT DEFAULT 0.1",
        "ALTER TABLE system_config ADD COLUMN IF NOT EXISTS vision_llm_max_tokens INTEGER DEFAULT 2000",
        "ALTER TABLE system_config ADD COLUMN IF NOT EXISTS vision_llm_timeout INTEGER DEFAULT 60",
        "ALTER TABLE kols ADD COLUMN IF NOT EXISTS vision_llm_enabled BOOLEAN DEFAULT FALSE",
        "ALTER TABLE kol_follows ADD COLUMN IF NOT EXISTS cooldown_reset_at TIMESTAMP WITH TIME ZONE",
        "ALTER TABLE customers ADD COLUMN IF NOT EXISTS multi_exchange_allowed BOOLEAN DEFAULT FALSE",
        "ALTER TABLE customers ADD COLUMN IF NOT EXISTS max_order_usdt FLOAT DEFAULT 5000.0",
        "ALTER TABLE customers ADD COLUMN IF NOT EXISTS emergency_stop BOOLEAN DEFAULT FALSE",
        "ALTER TABLE exchange_accounts ADD COLUMN IF NOT EXISTS api_key_hash VARCHAR(64) DEFAULT ''",
        "CREATE UNIQUE INDEX IF NOT EXISTS idx_exchange_accounts_api_key_hash_active ON exchange_accounts(api_key_hash) WHERE is_active = TRUE AND api_key_hash != ''",
        "ALTER TABLE positions ADD COLUMN IF NOT EXISTS entry_fee FLOAT DEFAULT 0.0",
        "ALTER TABLE exchange_accounts ADD COLUMN IF NOT EXISTS follow_enabled BOOLEAN DEFAULT FALSE",
        "ALTER TABLE exchange_accounts ADD COLUMN IF NOT EXISTS follow_weight FLOAT DEFAULT 1.0",
        "ALTER TABLE exchange_accounts ADD COLUMN IF NOT EXISTS max_order_usdt FLOAT DEFAULT 0.0",
        "ALTER TABLE exchange_accounts ADD COLUMN IF NOT EXISTS strategy_id INTEGER",
        "CREATE INDEX IF NOT EXISTS ix_exchange_accounts_follow_enabled ON exchange_accounts(follow_enabled)",
        "CREATE INDEX IF NOT EXISTS ix_exchange_accounts_strategy_id ON exchange_accounts(strategy_id)",
        "ALTER TABLE orders ADD COLUMN IF NOT EXISTS exchange_account_id INTEGER",
        "ALTER TABLE positions ADD COLUMN IF NOT EXISTS exchange_account_id INTEGER",
        "ALTER TABLE trades ADD COLUMN IF NOT EXISTS exchange_account_id INTEGER",
        "ALTER TABLE pending_orders ADD COLUMN IF NOT EXISTS exchange_account_id INTEGER",
        "CREATE INDEX IF NOT EXISTS ix_orders_exchange_account_id ON orders(exchange_account_id)",
        "CREATE INDEX IF NOT EXISTS ix_positions_exchange_account_id ON positions(exchange_account_id)",
        "CREATE INDEX IF NOT EXISTS ix_trades_exchange_account_id ON trades(exchange_account_id)",
        "CREATE INDEX IF NOT EXISTS ix_pending_orders_exchange_account_id ON pending_orders(exchange_account_id)",
        "ALTER TABLE customers ADD COLUMN IF NOT EXISTS customer_type VARCHAR(16) DEFAULT 'normal'",
        "ALTER TABLE customers ADD COLUMN IF NOT EXISTS show_signal_summary BOOLEAN DEFAULT FALSE",
        "ALTER TABLE customers ADD COLUMN IF NOT EXISTS invite_code VARCHAR(16) UNIQUE",
        "ALTER TABLE customers ADD COLUMN IF NOT EXISTS invited_by INTEGER REFERENCES customers(id)",
        "UPDATE customers SET invite_code = UPPER(SUBSTRING(MD5(RANDOM()::TEXT || username), 1, 8)) WHERE invite_code IS NULL",
        "CREATE INDEX IF NOT EXISTS idx_customers_customer_type ON customers(customer_type)",
        "CREATE INDEX IF NOT EXISTS idx_customers_invited_by ON customers(invited_by)",
        "CREATE TABLE IF NOT EXISTS discord_accounts (id SERIAL PRIMARY KEY, label VARCHAR(64) NOT NULL DEFAULT '默认 Discord 账号', token_enc TEXT NOT NULL, token_hash VARCHAR(64) NOT NULL DEFAULT '', enabled BOOLEAN NOT NULL DEFAULT TRUE, is_default BOOLEAN NOT NULL DEFAULT FALSE, last_error TEXT NOT NULL DEFAULT '', last_connected_at TIMESTAMP WITH TIME ZONE, created_at TIMESTAMP WITH TIME ZONE NOT NULL DEFAULT now(), updated_at TIMESTAMP WITH TIME ZONE NOT NULL DEFAULT now())",
        "ALTER TABLE discord_accounts ADD COLUMN IF NOT EXISTS token_hash VARCHAR(64) DEFAULT ''",
        "ALTER TABLE discord_accounts ADD COLUMN IF NOT EXISTS enabled BOOLEAN DEFAULT TRUE",
        "ALTER TABLE discord_accounts ADD COLUMN IF NOT EXISTS is_default BOOLEAN DEFAULT FALSE",
        "ALTER TABLE discord_accounts ADD COLUMN IF NOT EXISTS last_error TEXT DEFAULT ''",
        "ALTER TABLE discord_accounts ADD COLUMN IF NOT EXISTS last_connected_at TIMESTAMP WITH TIME ZONE",
        "CREATE INDEX IF NOT EXISTS ix_discord_accounts_enabled ON discord_accounts(enabled)",
        "CREATE INDEX IF NOT EXISTS ix_discord_accounts_is_default ON discord_accounts(is_default)",
        "CREATE INDEX IF NOT EXISTS ix_discord_accounts_token_hash ON discord_accounts(token_hash)",
        "ALTER TABLE kols ADD COLUMN IF NOT EXISTS discord_account_id INTEGER",
        "CREATE INDEX IF NOT EXISTS ix_kols_discord_account_id ON kols(discord_account_id)",
        "DO $$ BEGIN IF NOT EXISTS (SELECT 1 FROM pg_constraint WHERE conname = 'fk_kols_discord_account_id_discord_accounts') THEN ALTER TABLE kols ADD CONSTRAINT fk_kols_discord_account_id_discord_accounts FOREIGN KEY(discord_account_id) REFERENCES discord_accounts(id) ON DELETE SET NULL; END IF; END $$",
        "DO $$ BEGIN IF EXISTS (SELECT 1 FROM information_schema.columns WHERE table_name = 'system_config' AND column_name = 'llm_provider') THEN UPDATE system_config SET text_llm_provider = llm_provider WHERE text_llm_provider = 'deepseek' AND llm_provider IS NOT NULL AND llm_provider != ''; END IF; END $$",
        "DO $$ BEGIN IF EXISTS (SELECT 1 FROM information_schema.columns WHERE table_name = 'system_config' AND column_name = 'llm_api_key_enc') THEN UPDATE system_config SET text_llm_api_key_enc = llm_api_key_enc WHERE text_llm_api_key_enc = '' AND llm_api_key_enc IS NOT NULL AND llm_api_key_enc != ''; END IF; END $$",
        "DO $$ BEGIN IF EXISTS (SELECT 1 FROM information_schema.columns WHERE table_name = 'system_config' AND column_name = 'llm_model') THEN UPDATE system_config SET text_llm_model = llm_model WHERE text_llm_model = '' AND llm_model IS NOT NULL AND llm_model != ''; END IF; END $$",
        "DO $$ BEGIN IF EXISTS (SELECT 1 FROM information_schema.columns WHERE table_name = 'system_config' AND column_name = 'llm_api_base') THEN UPDATE system_config SET text_llm_api_base = llm_api_base WHERE text_llm_api_base = '' AND llm_api_base IS NOT NULL AND llm_api_base != ''; END IF; END $$",
        "DO $$ BEGIN IF EXISTS (SELECT 1 FROM information_schema.columns WHERE table_name = 'kols' AND column_name = 'llm_image_analysis') THEN UPDATE kols SET vision_llm_enabled = llm_image_analysis WHERE vision_llm_enabled = FALSE AND llm_image_analysis = TRUE; END IF; END $$",
        "WITH ranked AS (SELECT id, ROW_NUMBER() OVER (PARTITION BY customer_id ORDER BY is_default DESC, (last_error = '') DESC, last_verified_at DESC NULLS LAST, id ASC) AS rn FROM exchange_accounts WHERE is_active = TRUE) UPDATE exchange_accounts ea SET is_default = (ranked.rn = 1) FROM ranked WHERE ea.id = ranked.id",
        "UPDATE exchange_accounts SET follow_enabled = TRUE WHERE is_active = TRUE AND is_default = TRUE",
        "WITH chosen AS (SELECT DISTINCT ON (customer_id, exchange, testnet) id, customer_id, exchange, testnet FROM exchange_accounts WHERE is_active = TRUE ORDER BY customer_id, exchange, testnet, is_default DESC, (last_error = '') DESC, last_verified_at DESC NULLS LAST, id ASC) UPDATE positions p SET exchange_account_id = c.id FROM chosen c WHERE p.exchange_account_id IS NULL AND p.customer_id = c.customer_id AND p.exchange = c.exchange",
        "UPDATE orders o SET exchange_account_id = p.exchange_account_id FROM positions p WHERE o.exchange_account_id IS NULL AND o.position_id = p.id AND p.exchange_account_id IS NOT NULL",
        "WITH chosen AS (SELECT DISTINCT ON (customer_id, exchange, testnet) id, customer_id, exchange, testnet FROM exchange_accounts WHERE is_active = TRUE ORDER BY customer_id, exchange, testnet, is_default DESC, (last_error = '') DESC, last_verified_at DESC NULLS LAST, id ASC) UPDATE orders o SET exchange_account_id = c.id FROM chosen c WHERE o.exchange_account_id IS NULL AND o.customer_id = c.customer_id AND o.exchange = c.exchange",
        "UPDATE trades t SET exchange_account_id = p.exchange_account_id FROM positions p WHERE t.exchange_account_id IS NULL AND t.position_id = p.id AND p.exchange_account_id IS NOT NULL",
        "WITH chosen AS (SELECT DISTINCT ON (customer_id, exchange, testnet) id, customer_id, exchange, testnet FROM exchange_accounts WHERE is_active = TRUE ORDER BY customer_id, exchange, testnet, is_default DESC, (last_error = '') DESC, last_verified_at DESC NULLS LAST, id ASC) UPDATE pending_orders po SET exchange_account_id = c.id FROM chosen c WHERE po.exchange_account_id IS NULL AND po.customer_id = c.customer_id AND po.exchange = c.exchange",
    ]
    for sql in statements:
        op.execute(sa.text(sql))


def downgrade() -> None:
    # This migration consolidates legacy forward-only production DDL.
    pass
