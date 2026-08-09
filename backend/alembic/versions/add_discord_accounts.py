"""添加 Discord 多账号与 KOL 绑定

Revision ID: add_discord_accounts
Revises: add_symbol_notional_config
Create Date: 2026-08-09 00:00:00.000000
"""
from typing import Sequence, Union

from alembic import context, op
import sqlalchemy as sa


revision: str = "add_discord_accounts"
down_revision: Union[str, None] = "add_symbol_notional_config"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table(
        "discord_accounts",
        sa.Column("id", sa.Integer(), autoincrement=True, nullable=False),
        sa.Column("label", sa.String(length=64), nullable=False, server_default="默认 Discord 账号"),
        sa.Column("token_enc", sa.Text(), nullable=False),
        sa.Column("token_hash", sa.String(length=64), nullable=False, server_default=""),
        sa.Column("enabled", sa.Boolean(), nullable=False, server_default=sa.text("true")),
        sa.Column("is_default", sa.Boolean(), nullable=False, server_default=sa.text("false")),
        sa.Column("last_error", sa.Text(), nullable=False, server_default=""),
        sa.Column("last_connected_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index(op.f("ix_discord_accounts_enabled"), "discord_accounts", ["enabled"])
    op.create_index(op.f("ix_discord_accounts_id"), "discord_accounts", ["id"])
    op.create_index(op.f("ix_discord_accounts_is_default"), "discord_accounts", ["is_default"])
    op.create_index(op.f("ix_discord_accounts_token_hash"), "discord_accounts", ["token_hash"])

    op.add_column("kols", sa.Column("discord_account_id", sa.Integer(), nullable=True))
    op.create_index(op.f("ix_kols_discord_account_id"), "kols", ["discord_account_id"])
    op.create_foreign_key(
        "fk_kols_discord_account_id_discord_accounts",
        "kols",
        "discord_accounts",
        ["discord_account_id"],
        ["id"],
        ondelete="SET NULL",
    )

    # 兼容旧配置:若 system_config 里已有单 Token,复制为默认 Discord 账号。
    # 离线 SQL 模式无法读取旧数据,只在线迁移时执行该数据迁移。
    if not context.is_offline_mode():
        conn = op.get_bind()
        legacy = conn.execute(sa.text(
            "SELECT discord_token_enc FROM system_config "
            "WHERE id = 1 AND COALESCE(discord_token_enc, '') <> ''"
        )).first()
        if legacy:
            existing = conn.execute(sa.text("SELECT id FROM discord_accounts LIMIT 1")).first()
            if not existing:
                conn.execute(sa.text(
                    "INSERT INTO discord_accounts "
                    "(label, token_enc, token_hash, enabled, is_default, last_error, created_at, updated_at) "
                    "VALUES "
                    "('默认 Discord 账号', :token_enc, '', true, true, '', now(), now())"
                ), {"token_enc": legacy[0]})


def downgrade() -> None:
    op.drop_constraint("fk_kols_discord_account_id_discord_accounts", "kols", type_="foreignkey")
    op.drop_index(op.f("ix_kols_discord_account_id"), table_name="kols")
    op.drop_column("kols", "discord_account_id")

    op.drop_index(op.f("ix_discord_accounts_token_hash"), table_name="discord_accounts")
    op.drop_index(op.f("ix_discord_accounts_is_default"), table_name="discord_accounts")
    op.drop_index(op.f("ix_discord_accounts_id"), table_name="discord_accounts")
    op.drop_index(op.f("ix_discord_accounts_enabled"), table_name="discord_accounts")
    op.drop_table("discord_accounts")
