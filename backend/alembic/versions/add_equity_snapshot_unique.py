"""
Alembic 迁移: 为 equity_snapshots 添加唯一索引

Revision ID: add_equity_snapshot_unique
Revises: revert_numeric_to_float
Create Date: 2026-08-15

注意: 应用代码已将 snapshot_at 截断到分钟级(.replace(second=0, microsecond=0)),
因此直接对 (customer_id, exchange_account_id, snapshot_at) 建普通唯一索引,
无需使用 date_trunc 表达式索引(避免 IMMUTABLE 限制)。
"""
from alembic import op

revision = "add_equity_snapshot_unique"
down_revision = "revert_numeric_to_float"
branch_labels = None
depends_on = None


def upgrade() -> None:
    # 1. 清理已有的重复数据(同 customer + exchange_account + 分钟桶保留 id 最小的一条)
    op.execute("""
        DELETE FROM equity_snapshots a USING equity_snapshots b
        WHERE a.id > b.id
          AND a.customer_id = b.customer_id
          AND COALESCE(a.exchange_account_id, 0) = COALESCE(b.exchange_account_id, 0)
          AND date_trunc('minute', a.snapshot_at) = date_trunc('minute', b.snapshot_at)
    """)

    # 2. 创建唯一索引(应用层已截断 snapshot_at 到分钟,直接用列索引)
    op.execute("""
        CREATE UNIQUE INDEX IF NOT EXISTS ix_equity_snapshots_unique
        ON equity_snapshots (customer_id, exchange_account_id, snapshot_at)
    """)


def downgrade() -> None:
    op.execute("DROP INDEX IF EXISTS ix_equity_snapshots_unique")
