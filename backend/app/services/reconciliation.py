"""交易所对账服务:定期比对本地数据库与交易所实际持仓/挂单,检测并修复差异。

对账维度:
  1. 持仓对账(reconcile_positions)
     - 幽灵持仓:本地 DB 有 open master 仓位,交易所无对应持仓 → 自动标记 closed
     - 孤儿持仓:交易所有持仓,本地 DB 无记录 → 告警(无法自动创建,缺 KOL 关联)
     - 数量不一致:本地 master qty ≠ 交易所实际数量 → 告警(不自动修改,需人工确认)

  2. 挂单对账(reconcile_orders)
     - 幽灵挂单:本地 DB 有 pending 订单(含 exchange_order_id),交易所无对应挂单 → 标记 cancelled
     - 孤儿挂单:交易所有挂单,本地 DB 无记录 → 告警

自动修复策略:
  - 幽灵持仓:将 master 及其所有子仓位标记为 closed(qty=0),记录对账日志
  - 幽灵挂单:将订单状态改为 cancelled,记录对账日志
  - 数量不一致/孤儿:仅告警,不自动修改

安全原则:
  - 交易所 API 调用失败时,跳过该客户对账(不误判为幽灵)
  - 每个客户独立 session,失败隔离
  - 所有自动修复操作记录到 audit_logs
"""
from __future__ import annotations

import asyncio
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any

from loguru import logger
from sqlalchemy import select, update as sa_update

from app.core.database import AsyncSessionLocal
from app.models.audit import AuditLog
from app.models.config import ExchangeAccount
from app.models.trading import Order, Position
from app.services import exchange_adapter
from app.services.notification import notify

# 对账结果缓存(供 API 快速获取最近一次对账报告)
_last_report: "ReconciliationReport | None" = None


@dataclass
class PositionDiscrepancy:
    """持仓差异记录。"""
    type: str  # ghost_position | orphan_position | qty_mismatch
    customer_id: int
    exchange: str
    symbol: str
    side: str
    local_qty: float
    exchange_qty: float
    position_id: int | None = None  # 本地 position.id(幽灵/数量不一致时有值)
    detail: str = ""


@dataclass
class OrderDiscrepancy:
    """挂单差异记录。"""
    type: str  # ghost_order | orphan_order
    customer_id: int
    exchange: str
    symbol: str
    order_id: int | None = None  # 本地 order.id
    exchange_order_id: str = ""
    detail: str = ""


@dataclass
class ReconciliationReport:
    """对账报告。"""
    started_at: datetime = field(default_factory=lambda: datetime.now(timezone.utc))
    finished_at: datetime | None = None
    total_accounts_checked: int = 0
    total_accounts_failed: int = 0
    position_discrepancies: list[PositionDiscrepancy] = field(default_factory=list)
    order_discrepancies: list[OrderDiscrepancy] = field(default_factory=list)
    auto_fixed: int = 0  # 自动修复数量
    errors: list[str] = field(default_factory=list)

    @property
    def has_issues(self) -> bool:
        return bool(self.position_discrepancies or self.order_discrepancies)

    def to_dict(self) -> dict[str, Any]:
        return {
            "started_at": self.started_at.isoformat() if self.started_at else None,
            "finished_at": self.finished_at.isoformat() if self.finished_at else None,
            "total_accounts_checked": self.total_accounts_checked,
            "total_accounts_failed": self.total_accounts_failed,
            "auto_fixed": self.auto_fixed,
            "position_discrepancies": [
                {
                    "type": d.type,
                    "customer_id": d.customer_id,
                    "exchange": d.exchange,
                    "symbol": d.symbol,
                    "side": d.side,
                    "local_qty": d.local_qty,
                    "exchange_qty": d.exchange_qty,
                    "position_id": d.position_id,
                    "detail": d.detail,
                }
                for d in self.position_discrepancies
            ],
            "order_discrepancies": [
                {
                    "type": d.type,
                    "customer_id": d.customer_id,
                    "exchange": d.exchange,
                    "symbol": d.symbol,
                    "order_id": d.order_id,
                    "exchange_order_id": d.exchange_order_id,
                    "detail": d.detail,
                }
                for d in self.order_discrepancies
            ],
            "errors": self.errors,
        }


def _normalize_symbol_for_match(symbol: str) -> str:
    """将交易所返回的 symbol 标准化为本地 DB 格式。

    OKX 返回 "BTC/USDT:USDT" → "BTC/USDT"
    Binance/Bybit 返回 "BTC/USDT" → "BTC/USDT"
    """
    if not symbol:
        return ""
    # 去掉 ":USDT" 后缀
    if ":" in symbol:
        symbol = symbol.split(":")[0]
    return symbol.upper()


def _exchange_position_qty(pos: dict) -> float:
    """从 ccxt 持仓字典中计算实际币数。

    ccxt 返回的 position:
      - contracts: 合约张数
      - contractSize: 每张合约的币数(OKX BTC 合约 = 0.01)
      - side: 'long' | 'short'

    实际币数 = contracts * contractSize
    如果 contractSize 缺失,则直接用 contracts(即币数)。
    """
    contracts = float(pos.get("contracts", 0) or 0)
    contract_size = float(pos.get("contractSize", 0) or 0)
    if contract_size > 0:
        return contracts * contract_size
    return contracts


def _is_known_auth_error(message: str | None) -> bool:
    """识别需要人工重新配置 API Key/IP/权限的交易所认证错误。"""
    if not message:
        return False
    msg = message.lower()
    return (
        "invalid api-key" in msg
        or "api-key" in msg and "permission" in msg
        or "ip" in msg and "permission" in msg
        or "authentication" in msg
        or "unauthorized" in msg
    )


async def reconcile_positions(
    db,
    customer_id: int,
    exchange: str,
    exchange_account_id: int | None,
    ex,
    report: ReconciliationReport,
) -> None:
    """对账单个客户+交易所的持仓。

    比对逻辑:
      1. 从交易所获取所有持仓(fetch_positions)
      2. 从本地 DB 获取所有 open master 仓位(parent_id IS NULL)
      3. 按 (symbol, side) 匹配,检测三类差异
    """
    # 1. 获取交易所持仓
    try:
        ex_positions = await exchange_adapter.fetch_positions(ex)
    except Exception as e:
        msg = f"获取交易所持仓失败 customer={customer_id} exchange={exchange}: {e}"
        logger.warning(msg)
        report.errors.append(msg)
        raise

    # 标准化交易所持仓: {(symbol, side): qty}
    ex_pos_map: dict[tuple[str, str], float] = {}
    for ep in ex_positions:
        sym = _normalize_symbol_for_match(ep.get("symbol", ""))
        side = (ep.get("side") or "").lower()
        qty = _exchange_position_qty(ep)
        if sym and side and qty > 0:
            # 同一 symbol+side 可能有多条(不同保证金模式),取合计
            ex_pos_map[(sym, side)] = ex_pos_map.get((sym, side), 0) + qty

    # 2. 获取本地 open master 仓位
    stmt = select(Position).where(
        Position.customer_id == customer_id,
        Position.exchange == exchange,
        Position.status == "open",
        Position.parent_id.is_(None),
    )
    if exchange_account_id is not None:
        stmt = stmt.where(Position.exchange_account_id == exchange_account_id)
    local_positions = (await db.execute(stmt)).scalars().all()

    # 3. 构建本地持仓映射: {(symbol, side): [Position, ...]}
    # 同一账号、同一 symbol+side 可能存在多个 KOL 主仓,交易所只有一个净持仓;
    # 对账时必须用本地主仓合计数量与交易所数量比较,不能只取第一条。
    local_pos_map: dict[tuple[str, str], list[Position]] = {}
    for pos in local_positions:
        key = (pos.symbol.upper(), pos.side.lower())
        local_pos_map.setdefault(key, []).append(pos)

    # 4. 检测幽灵持仓:本地有但交易所无
    matched_ex_keys: set[tuple[str, str]] = set()
    for key, positions in local_pos_map.items():
        ex_qty = ex_pos_map.get(key, 0)
        local_qty = sum(float(pos.qty or 0) for pos in positions)
        first_pos = positions[0]
        position_ids = ",".join(str(pos.id) for pos in positions)
        if ex_qty <= 0:
            # 幽灵持仓:交易所无此仓位
            for local_pos in positions:
                disc = PositionDiscrepancy(
                    type="ghost_position",
                    customer_id=customer_id,
                    exchange=exchange,
                    symbol=local_pos.symbol,
                    side=local_pos.side,
                    local_qty=local_pos.qty,
                    exchange_qty=0,
                    position_id=local_pos.id,
                    detail=f"本地 master 仓位 #{local_pos.id} 仍为 open,但交易所账号 #{exchange_account_id} 无此持仓(可能被手动平仓/强平/到期)",
                )
                report.position_discrepancies.append(disc)

                # 自动修复:标记 master 及所有子仓位为 closed(使用独立 session)
                await _fix_ghost_position(local_pos.id, local_pos.symbol, local_pos.side, report)
        else:
            matched_ex_keys.add(key)
            # 检测数量不一致(允许 1% 误差,考虑合约精度)
            if local_qty > 0:
                diff_ratio = abs(local_qty - ex_qty) / local_qty
                if diff_ratio > 0.01:
                    disc = PositionDiscrepancy(
                        type="qty_mismatch",
                        customer_id=customer_id,
                        exchange=exchange,
                        symbol=first_pos.symbol,
                        side=first_pos.side,
                        local_qty=local_qty,
                        exchange_qty=ex_qty,
                        position_id=first_pos.id,
                        detail=f"主仓位 #{position_ids} 聚合数量不一致: 交易所账号 #{exchange_account_id} 本地合计={local_qty} 交易所={ex_qty} (差异 {diff_ratio*100:.1f}%)",
                    )
                    report.position_discrepancies.append(disc)

    # 5. 检测孤儿持仓:交易所有但本地无
    for key, ex_qty in ex_pos_map.items():
        if key not in matched_ex_keys and key not in local_pos_map:
            sym, side = key
            disc = PositionDiscrepancy(
                type="orphan_position",
                customer_id=customer_id,
                exchange=exchange,
                symbol=sym,
                side=side,
                local_qty=0,
                exchange_qty=ex_qty,
                detail=f"交易所账号 #{exchange_account_id} 有 {sym} {side} 持仓 {ex_qty},但本地无 master 仓位记录(可能通过其他渠道开仓)",
            )
            report.position_discrepancies.append(disc)


async def _fix_ghost_position(
    master_pos_id: int,
    master_pos_symbol: str,
    master_pos_side: str,
    report: ReconciliationReport,
) -> None:
    """修复幽灵持仓:将 master 及其所有子仓位标记为 closed。

    使用独立事务,避免影响调用方 session。
    """
    now = datetime.now(timezone.utc)
    try:
        async with AsyncSessionLocal() as db:
            # 标记 master
            await db.execute(
                sa_update(Position)
                .where(Position.id == master_pos_id)
                .values(status="closed", qty=0, closed_at=now)
            )
            # 标记所有子仓位
            children = (
                await db.execute(
                    select(Position).where(
                        Position.parent_id == master_pos_id,
                        Position.status == "open",
                    )
                )
            ).scalars().all()
            for child in children:
                await db.execute(
                    sa_update(Position)
                    .where(Position.id == child.id)
                    .values(status="closed", qty=0, closed_at=now)
                )
            # 记录审计日志
            db.add(AuditLog(
                user_id=None,
                action="reconciliation_fix",
                target=f"position:{master_pos_id}",
                detail=f"对账自动修复: 持仓 #{master_pos_id} ({master_pos_symbol} {master_pos_side}) 交易所无持仓,已标记 closed (含 {len(children)} 个子仓位)",
            ))
            await db.commit()
            report.auto_fixed += 1 + len(children)
            logger.info(
                f"[对账] 幽灵持仓修复: pos={master_pos_id} {master_pos_symbol} {master_pos_side} "
                f"已标记 closed (含 {len(children)} 个子仓位)"
            )
    except Exception as e:
        logger.error(f"[对账] 幽灵持仓修复失败 pos={master_pos_id}: {e}")
        report.errors.append(f"幽灵持仓修复失败 pos={master_pos_id}: {e}")


async def reconcile_orders(
    db,
    customer_id: int,
    exchange: str,
    exchange_account_id: int | None,
    ex,
    report: ReconciliationReport,
) -> None:
    """对账单个客户+交易所的挂单。

    比对逻辑:
      1. 从交易所获取所有未成交挂单(fetch_open_orders)
      2. 从本地 DB 获取所有 pending 订单(含 exchange_order_id)
      3. 按 exchange_order_id 匹配,检测幽灵/孤儿挂单
    """
    # 1. 获取交易所挂单
    try:
        ex_orders = await ex.fetch_open_orders()
    except Exception as e:
        msg = f"获取交易所挂单失败 customer={customer_id} exchange={exchange}: {e}"
        logger.warning(msg)
        report.errors.append(msg)
        raise

    # 标准化交易所挂单 ID 集合
    ex_order_ids: set[str] = {str(o.get("id", "")) for o in ex_orders if o.get("id")}
    ex_order_map: dict[str, dict] = {str(o.get("id", "")): o for o in ex_orders if o.get("id")}

    # 2. 获取本地 pending 订单(有 exchange_order_id 的)
    stmt = select(Order).where(
        Order.customer_id == customer_id,
        Order.exchange == exchange,
        Order.status == "pending",
        Order.exchange_order_id != "",
        Order.deleted_at.is_(None),
    )
    if exchange_account_id is not None:
        stmt = stmt.where(Order.exchange_account_id == exchange_account_id)
    local_orders = (await db.execute(stmt)).scalars().all()

    # 3. 检测幽灵挂单:本地有 pending 但交易所无
    for order in local_orders:
        oid = order.exchange_order_id
        if oid and oid not in ex_order_ids:
            disc = OrderDiscrepancy(
                type="ghost_order",
                customer_id=customer_id,
                exchange=exchange,
                symbol=order.symbol,
                order_id=order.id,
                exchange_order_id=oid,
                detail=f"本地挂单 #{order.id} ({order.symbol}) 状态为 pending,但交易所账号 #{exchange_account_id} 无此挂单(可能已成交/撤单/过期)",
            )
            report.order_discrepancies.append(disc)
            # 自动修复:标记为 cancelled(使用独立 session)
            await _fix_ghost_order(order.id, order.symbol, order.exchange_order_id, report)

    # 4. 检测孤儿挂单:交易所有但本地无
    local_order_ids: set[str] = {o.exchange_order_id for o in local_orders if o.exchange_order_id}
    for oid, ex_order in ex_order_map.items():
        if oid not in local_order_ids:
            sym = _normalize_symbol_for_match(ex_order.get("symbol", ""))
            disc = OrderDiscrepancy(
                type="orphan_order",
                customer_id=customer_id,
                exchange=exchange,
                symbol=sym,
                exchange_order_id=oid,
                detail=f"交易所账号 #{exchange_account_id} 有挂单 {oid} ({sym}),但本地无 pending 记录(可能通过其他渠道下单)",
            )
            report.order_discrepancies.append(disc)


async def _fix_ghost_order(
    order_id: int,
    order_symbol: str,
    exchange_order_id: str,
    report: ReconciliationReport,
) -> None:
    """修复幽灵挂单:将订单状态改为 cancelled。

    使用独立事务,避免影响调用方 session。
    """
    try:
        async with AsyncSessionLocal() as db:
            await db.execute(
                sa_update(Order)
                .where(Order.id == order_id)
                .values(
                    status="cancelled",
                    error_msg=f"对账自动修复: 交易所无此挂单(原状态 pending → cancelled)",
                )
            )
            db.add(AuditLog(
                user_id=None,
                action="reconciliation_fix",
                target=f"order:{order_id}",
                detail=f"对账自动修复: 挂单 #{order_id} ({order_symbol} exchange_id={exchange_order_id}) 交易所无记录,已标记 cancelled",
            ))
            await db.commit()
            report.auto_fixed += 1
            logger.info(
                f"[对账] 幽灵挂单修复: order={order_id} {order_symbol} "
                f"exchange_order_id={exchange_order_id} 已标记 cancelled"
            )
    except Exception as e:
        logger.error(f"[对账] 幽灵挂单修复失败 order={order_id}: {e}")
        report.errors.append(f"幽灵挂单修复失败 order={order_id}: {e}")


async def _reconcile_one_account(
    exchange_account_id: int,
    customer_id: int,
    exchange: str,
    testnet: bool,
    report: ReconciliationReport,
) -> None:
    """对账单个客户+交易所账号(独立 session,失败隔离)。"""
    async with AsyncSessionLocal() as db:
        ex = None
        try:
            ex, _ = await exchange_adapter.load_exchange(
                db,
                customer_id,
                exchange,
                testnet,
                exchange_account_id=exchange_account_id,
            )
            report.total_accounts_checked += 1

            # 持仓对账
            await reconcile_positions(db, customer_id, exchange, exchange_account_id, ex, report)

            # 挂单对账
            await reconcile_orders(db, customer_id, exchange, exchange_account_id, ex, report)

        except Exception as e:
            raw_error = str(e)
            msg = f"对账失败 account={exchange_account_id} customer={customer_id} exchange={exchange}: {raw_error}"
            logger.warning(msg)
            report.errors.append(msg)
            report.total_accounts_failed += 1
            if _is_known_auth_error(raw_error):
                await db.execute(
                    sa_update(ExchangeAccount)
                    .where(ExchangeAccount.id == exchange_account_id)
                    .values(last_error=raw_error[:500])
                )
                await db.commit()
        finally:
            if ex:
                await exchange_adapter.close_exchange(ex)


async def run_reconciliation() -> ReconciliationReport:
    """执行全量对账:遍历所有活跃客户的交易所账号,比对持仓和挂单。

    Returns:
        ReconciliationReport 对账报告
    """
    global _last_report
    report = ReconciliationReport()
    logger.info("[对账] 开始执行交易所对账...")

    # 预取所有活跃交易所账号。对账必须按 exchange_account_id 逐个账号执行,
    # 否则多 API 账号会把本地仓位和交易所仓位混在一起,造成数量误报。
    async with AsyncSessionLocal() as db:
        rows = (
            await db.execute(
                select(
                    ExchangeAccount.id,
                    ExchangeAccount.customer_id,
                    ExchangeAccount.exchange,
                    ExchangeAccount.testnet,
                    ExchangeAccount.last_error,
                )
                .where(ExchangeAccount.is_active.is_(True))
                .order_by(ExchangeAccount.customer_id, ExchangeAccount.exchange, ExchangeAccount.id)
            )
        ).all()
        accounts = [
            (r.id, r.customer_id, r.exchange, r.testnet, r.last_error or "")
            for r in rows
        ]

    if not accounts:
        logger.info("[对账] 无活跃交易所账号,跳过")
        report.finished_at = datetime.now(timezone.utc)
        _last_report = report
        return report

    # 逐个账号对账(串行,避免交易所 API 限流)
    for exchange_account_id, customer_id, exchange, testnet, last_error in accounts:
        if _is_known_auth_error(last_error):
            msg = (
                f"跳过对账 account={exchange_account_id} customer={customer_id} "
                f"exchange={exchange}: API Key/IP/权限错误,需重新配置后再测试"
            )
            logger.info(msg)
            continue
        await _reconcile_one_account(exchange_account_id, customer_id, exchange, testnet, report)

    report.finished_at = datetime.now(timezone.utc)

    # 汇总日志
    ghost_pos = sum(1 for d in report.position_discrepancies if d.type == "ghost_position")
    orphan_pos = sum(1 for d in report.position_discrepancies if d.type == "orphan_position")
    qty_mismatch = sum(1 for d in report.position_discrepancies if d.type == "qty_mismatch")
    ghost_ord = sum(1 for d in report.order_discrepancies if d.type == "ghost_order")
    orphan_ord = sum(1 for d in report.order_discrepancies if d.type == "orphan_order")

    logger.info(
        f"[对账] 完成: 检查 {report.total_accounts_checked} 个账号, "
        f"失败 {report.total_accounts_failed}, "
        f"幽灵持仓 {ghost_pos}, 孤儿持仓 {orphan_pos}, 数量不一致 {qty_mismatch}, "
        f"幽灵挂单 {ghost_ord}, 孤儿挂单 {orphan_ord}, "
        f"自动修复 {report.auto_fixed} 项"
    )

    # 有差异时发送告警通知
    if report.has_issues:
        summary_lines = [
            f"检查账号数: {report.total_accounts_checked}",
            f"失败账号数: {report.total_accounts_failed}",
            f"自动修复数: {report.auto_fixed}",
            "",
        ]
        if ghost_pos:
            summary_lines.append(f"【幽灵持仓】{ghost_pos} 个(已自动标记 closed):")
            for d in report.position_discrepancies:
                if d.type == "ghost_position":
                    summary_lines.append(f"  - 客户#{d.customer_id} {d.exchange} {d.symbol} {d.side} 本地qty={d.local_qty}")
        if orphan_pos:
            summary_lines.append(f"【孤儿持仓】{orphan_pos} 个(交易所有持仓但本地无记录):")
            for d in report.position_discrepancies:
                if d.type == "orphan_position":
                    summary_lines.append(f"  - 客户#{d.customer_id} {d.exchange} {d.symbol} {d.side} 交易所qty={d.exchange_qty}")
        if qty_mismatch:
            summary_lines.append(f"【数量不一致】{qty_mismatch} 个(需人工确认):")
            for d in report.position_discrepancies:
                if d.type == "qty_mismatch":
                    summary_lines.append(f"  - 客户#{d.customer_id} {d.exchange} {d.symbol} {d.side} 本地={d.local_qty} 交易所={d.exchange_qty}")
        if ghost_ord:
            summary_lines.append(f"【幽灵挂单】{ghost_ord} 个(已自动标记 cancelled):")
            for d in report.order_discrepancies:
                if d.type == "ghost_order":
                    summary_lines.append(f"  - 客户#{d.customer_id} {d.exchange} {d.symbol} order_id={d.exchange_order_id}")
        if orphan_ord:
            summary_lines.append(f"【孤儿挂单】{orphan_ord} 个(交易所有挂单但本地无记录):")
            for d in report.order_discrepancies:
                if d.type == "orphan_order":
                    summary_lines.append(f"  - 客户#{d.customer_id} {d.exchange} {d.symbol} order_id={d.exchange_order_id}")

        try:
            await notify(
                "error", "交易所对账差异报告",
                "\n".join(summary_lines),
            )
        except Exception as e:
            logger.warning(f"[对账] 告警通知发送失败: {e}")

    _last_report = report
    return report


def get_last_report() -> ReconciliationReport | None:
    """获取最近一次对账报告(供 API 查询)。"""
    return _last_report
