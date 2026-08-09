"""分析服务:KOL 排行、账户净值快照与走势、信号汇总、交易统计。"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.config import EquitySnapshot
from app.models.kol import Kol
from app.models.signal import Signal
from app.models.trading import Position, Trade


async def kol_ranking(db: AsyncSession, days: int = 30) -> list[dict]:
    """KOL 排行榜:胜率、总盈亏、信号数、平均收益率。

    胜率按 Position 统计(每个子仓位从开到平算 1 次交易),避免分批止盈被重复计数。
    利润按 Trade 统计(按成交时间累加,准确反映时间段内的盈亏)。
    """
    since = datetime.now(timezone.utc) - timedelta(days=days)

    # 1. 利润:按 Trade 统计(按成交时间,时间段内累加)
    pnl_stmt = (
        select(
            Trade.kol_id,
            func.coalesce(func.sum(Trade.realized_pnl), 0.0).label("total_pnl"),
        )
        .where(Trade.is_close.is_(True), Trade.kol_id.isnot(None), Trade.executed_at >= since)
        .group_by(Trade.kol_id)
    )
    pnl_rows = {r.kol_id: float(r.total_pnl) for r in (await db.execute(pnl_stmt)).all()}

    # 2. 胜率和交易数:按 Position 统计(只算已平仓的子仓位,避免分批止盈重复计数)
    pos_stmt = (
        select(
            Position.kol_id,
            func.count(Position.id).label("trade_count"),
            func.count(Position.id).filter(Position.realized_pnl > 0).label("win_count"),
        )
        .where(
            Position.status == "closed",
            Position.kol_id.isnot(None),
            Position.parent_id.is_not(None),  # 只算子仓位(KOL 维度)
            Position.closed_at >= since,
        )
        .group_by(Position.kol_id)
    )
    pos_rows_list = (await db.execute(pos_stmt)).all()
    pos_rows = {r.kol_id: r for r in pos_rows_list}

    # 信号数
    sig_stmt = (
        select(Signal.kol_id, func.count(Signal.id).label("signal_count"))
        .where(Signal.received_at >= since)
        .group_by(Signal.kol_id)
    )
    sig_rows = {r.kol_id: r.signal_count for r in (await db.execute(sig_stmt)).all()}

    # KOL 名称
    kols = {k.id: k for k in (await db.execute(select(Kol))).scalars().all()}

    # 合并:以 Position 统计为主(有交易记录的 KOL),补利润
    all_kol_ids = set(pos_rows.keys()) | set(pnl_rows.keys())
    ranking = []
    for kol_id in all_kol_ids:
        kol = kols.get(kol_id)
        pos_row = pos_rows.get(kol_id)
        trade_count = pos_row.trade_count if pos_row else 0
        win_count = pos_row.win_count if pos_row else 0
        total_pnl = pnl_rows.get(kol_id, 0.0)
        win_rate = (win_count / trade_count * 100) if trade_count else 0
        ranking.append({
            "kol_id": kol_id,
            "kol_name": kol.name if kol else "未知",
            "avatar": kol.avatar if kol else "",
            "trade_count": trade_count,
            "signal_count": sig_rows.get(kol_id, 0),
            "win_rate": round(win_rate, 2),
            "total_pnl": round(total_pnl, 2),
        })
    ranking.sort(key=lambda x: x["total_pnl"], reverse=True)

    # 补充无成交但被关注的 KOL
    for kol in kols.values():
        if not any(x["kol_id"] == kol.id for x in ranking):
            ranking.append({
                "kol_id": kol.id,
                "kol_name": kol.name,
                "avatar": kol.avatar,
                "trade_count": 0,
                "signal_count": sig_rows.get(kol.id, 0),
                "win_rate": 0.0,
                "total_pnl": 0.0,
            })
    return ranking


async def take_equity_snapshot(db: AsyncSession, customer_id: int, exchange: str) -> None:
    """记录一条净值快照(由定时任务调用)。"""
    from app.services import exchange_adapter

    try:
        ex, _ = await exchange_adapter.load_exchange(db, customer_id, exchange)
        try:
            bal = await exchange_adapter.fetch_balance(ex)
        finally:
            await exchange_adapter.close_exchange(ex)
        snapshot = EquitySnapshot(
            customer_id=customer_id,
            exchange=exchange,
            equity=float(bal.get("equity", 0)),
            balance=float(bal.get("balance", 0)),
            unrealized_pnl=0.0,
            snapshot_at=datetime.now(timezone.utc),
        )
        db.add(snapshot)
        await db.commit()
    except Exception as e:
        from loguru import logger

        logger.warning(f"净值快照失败 customer={customer_id} exchange={exchange}: {e}")
        await db.rollback()


async def equity_curve(
    db: AsyncSession, customer_id: int, exchange: str | None = None, days: int = 30
) -> list[dict]:
    since = datetime.now(timezone.utc) - timedelta(days=days)
    stmt = (
        select(EquitySnapshot)
        .where(EquitySnapshot.customer_id == customer_id, EquitySnapshot.snapshot_at >= since)
        .order_by(EquitySnapshot.snapshot_at.asc())
    )
    if exchange:
        stmt = stmt.where(EquitySnapshot.exchange == exchange)
    rows = (await db.execute(stmt)).scalars().all()
    return [
        {
            "snapshot_at": r.snapshot_at.isoformat() if r.snapshot_at else None,
            "equity": r.equity,
            "balance": r.balance,
            "unrealized_pnl": r.unrealized_pnl,
        }
        for r in rows
    ]


async def dashboard_stats(db: AsyncSession, customer_id: int) -> dict:
    """仪表盘关键指标。

    胜率和交易次数按 Position 统计(每个子仓位从开到平算 1 次),避免分批止盈重复计数。
    利润按 Trade 统计(按成交时间累加,准确反映当日/累计盈亏)。
    """
    today = datetime.now(timezone.utc).replace(hour=0, minute=0, second=0, microsecond=0)
    # 持仓数:只算 master 仓位
    open_positions = (
        await db.execute(
            select(func.count(Position.id)).where(
                Position.customer_id == customer_id,
                Position.status == "open",
                Position.parent_id.is_(None),
            )
        )
    ).scalar_one()
    # 利润:按 Trade 统计(按成交时间)
    today_pnl = (
        await db.execute(
            select(func.coalesce(func.sum(Trade.realized_pnl), 0.0)).where(
                Trade.customer_id == customer_id,
                Trade.is_close.is_(True),
                Trade.executed_at >= today,
            )
        )
    ).scalar_one()
    total_pnl = (
        await db.execute(
            select(func.coalesce(func.sum(Trade.realized_pnl), 0.0)).where(
                Trade.customer_id == customer_id, Trade.is_close.is_(True)
            )
        )
    ).scalar_one()
    # 交易数和胜率:按 Position 统计(已平仓的子仓位,避免分批止盈重复计数)
    total_trades = (
        await db.execute(
            select(func.count(Position.id)).where(
                Position.customer_id == customer_id,
                Position.status == "closed",
                Position.parent_id.is_not(None),  # 只算子仓位
            )
        )
    ).scalar_one()
    win_trades = (
        await db.execute(
            select(func.count(Position.id)).where(
                Position.customer_id == customer_id,
                Position.status == "closed",
                Position.parent_id.is_not(None),
                Position.realized_pnl > 0,
            )
        )
    ).scalar_one()
    win_rate = (win_trades / total_trades * 100) if total_trades else 0
    return {
        "open_positions": open_positions,
        "today_pnl": round(float(today_pnl), 2),
        "total_pnl": round(float(total_pnl), 2),
        "total_trades": total_trades,
        "win_rate": round(win_rate, 2),
    }
