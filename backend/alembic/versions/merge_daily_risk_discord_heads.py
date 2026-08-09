# Restore missing Alembic merge revision referenced by existing production database.
#
# Revision ID: merge_daily_risk_discord_heads
# Revises: add_daily_risk_snapshots, add_discord_accounts
# Create Date: 2026-08-09
from typing import Sequence, Union


revision: str = "merge_daily_risk_discord_heads"
down_revision: Union[str, Sequence[str], None] = ("add_daily_risk_snapshots", "add_discord_accounts")
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    pass


def downgrade() -> None:
    pass
