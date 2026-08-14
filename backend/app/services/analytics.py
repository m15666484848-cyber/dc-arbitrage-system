"""分析服务:KOL 排行、账户净值快照与走势、信号汇总、交易统计。"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone

from loguru import logger
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.config import EquitySnapshot, ExchangeAccount
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
    ranking.sort(key=lambda x: (x["win_rate"], x["trade_count"], x["total_pnl"]), reverse=True)

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


async def take_equity_snapshot(
    db: AsyncSession,
    customer_id: int,
    exchange: str,
    testnet: bool | None = None,
    exchange_account_id: int | None = None,
) -> None:
    """记录一条净值快照(由定时任务调用)。"""
    from app.services import exchange_adapter

    try:
        ex, _ = await exchange_adapter.load_exchange(
            db,
            customer_id,
            exchange,
            testnet,
            exchange_account_id=exchange_account_id,
        )
        try:
            bal = await exchange_adapter.fetch_balance(ex)
        finally:
            await exchange_adapter.close_exchange(ex)

        # SU-S2 修复: 查询所有未平仓持仓的未实现盈亏并累加
        total_unrealized = 0.0
        try:
            from app.services.position_manager import _get_cached_price, compute_pnl

            open_positions = (
                await db.execute(
                    select(Position).where(
                        Position.customer_id == customer_id,
                        Position.exchange == exchange,
                        Position.status == "open",
                    )
                )
            ).scalars().all()
            for pos in open_positions:
                # 如果指定了 exchange_account_id, 只统计该账号的持仓
                if exchange_account_id is not None and pos.exchange_account_id != exchange_account_id:
                    continue
                try:
                    current_price = await _get_cached_price(pos.exchange, pos.symbol)
                    if not current_price or current_price <= 0:
                        current_price = await exchange_adapter.fetch_market_price(
                            pos.exchange, pos.symbol
                        )
                    if current_price and current_price > 0:
                        pnl, _ = compute_pnl(pos, current_price)
                        total_unrealized += pnl
                except Exception as e:
                    logger.warning(f"计算仓位 {pos.id} 未实现盈亏失败: {e}")
        except Exception as e:
            logger.warning(f"获取持仓列表失败 customer={customer_id}: {e}")

        snapshot = EquitySnapshot(
            customer_id=customer_id,
            exchange_account_id=exchange_account_id,
            exchange=exchange,
            equity=float(bal.get("equity", 0)),
            balance=float(bal.get("balance", 0)),
            unrealized_pnl=total_unrealized,
            snapshot_at=datetime.now(timezone.utc),
        )
        db.add(snapshot)
        try:
            await db.commit()
        except Exception:
            await db.rollback()
            logger.exception("db commit failed")
            raise
    except Exception as e:
        logger.warning(f"净值快照失败 customer={customer_id} exchange={exchange} testnet={testnet}: {e}")
        logger.exception(f"净值快照完整堆栈 customer={customer_id} exchange={exchange}: {e}")
        await db.rollback()


def _snapshot_bucket(dt: datetime) -> datetime:
    """按 5 分钟聚合快照，避免多 API 账号快照交替造成假回撤。"""
    return dt.replace(minute=(dt.minute // 5) * 5, second=0, microsecond=0)


async def _snapshot_points(
    db: AsyncSession,
    customer_id: int,
    exchange: str | None = None,
    days: int = 30,
    exchange_account_id: int | None = None,
) -> list[dict]:
    since = datetime.now(timezone.utc) - timedelta(days=days)
    stmt = (
        select(EquitySnapshot)
        .where(EquitySnapshot.customer_id == customer_id, EquitySnapshot.snapshot_at >= since)
        .order_by(EquitySnapshot.snapshot_at.asc())
    )
    fallback_exchange: str | None = None
    if exchange_account_id:
        stmt = stmt.where(EquitySnapshot.exchange_account_id == exchange_account_id)
        rows = (await db.execute(stmt)).scalars().all()
        # 老快照没有 exchange_account_id，按该 API 的交易所做只读兜底，避免切换后显示空白。
        if not rows:
            acc = (
                await db.execute(
                    select(ExchangeAccount).where(
                        ExchangeAccount.id == exchange_account_id,
                        ExchangeAccount.customer_id == customer_id,
                    )
                )
            ).scalar_one_or_none()
            fallback_exchange = acc.exchange if acc else None
        else:
            return [
                {
                    "snapshot_at": r.snapshot_at,
                    "equity": float(r.equity or 0),
                    "balance": float(r.balance or r.equity or 0),
                    "unrealized_pnl": float(r.unrealized_pnl or 0),
                }
                for r in rows
            ]
    if fallback_exchange:
        stmt = (
            select(EquitySnapshot)
            .where(
                EquitySnapshot.customer_id == customer_id,
                EquitySnapshot.snapshot_at >= since,
                EquitySnapshot.exchange == fallback_exchange,
            )
            .order_by(EquitySnapshot.snapshot_at.asc())
        )
    if exchange:
        stmt = stmt.where(EquitySnapshot.exchange == exchange)
    rows = (await db.execute(stmt)).scalars().all()

    if not exchange_account_id:
        # 只统计当前激活的交易所账户,避免已禁用账户的旧快照导致余额虚高
        active_acc_ids = (await db.execute(
            select(ExchangeAccount.id).where(
                ExchangeAccount.customer_id == customer_id,
                ExchangeAccount.is_active.is_(True),
            )
        )).scalars().all()
        if active_acc_ids:
            rows = [r for r in rows if r.exchange_account_id in active_acc_ids]
        # 多 API 聚合不能简单按时间桶求和。
        # 快照通常是逐个 API 账号轮询写入的，如果某个时间桶只包含部分账号，
        # 会把“账号补齐后的权益增加”误判为真实收益/回撤。
        # 因此这里按账号做前值补齐，并且只从所有账号都有首个快照后开始输出合计曲线。
        account_ids = sorted({int(r.exchange_account_id) for r in rows if r.exchange_account_id})
        if len(account_ids) > 1:
            grouped: dict[datetime, list[EquitySnapshot]] = {}
            for r in rows:
                if not r.snapshot_at or not r.exchange_account_id:
                    continue
                grouped.setdefault(_snapshot_bucket(r.snapshot_at), []).append(r)

            latest_by_account: dict[int, dict] = {}
            points: list[dict] = []
            for key in sorted(grouped):
                for r in grouped[key]:
                    latest_by_account[int(r.exchange_account_id)] = {
                        "equity": float(r.equity or 0),
                        "balance": float(r.balance or r.equity or 0),
                        "unrealized_pnl": float(r.unrealized_pnl or 0),
                    }
                if all(account_id in latest_by_account for account_id in account_ids):
                    points.append({
                        "snapshot_at": key,
                        "equity": sum(v["equity"] for v in latest_by_account.values()),
                        "balance": sum(v["balance"] for v in latest_by_account.values()),
                        "unrealized_pnl": sum(v["unrealized_pnl"] for v in latest_by_account.values()),
                    })
            return points

        buckets: dict[datetime, dict] = {}
        for r in rows:
            if not r.snapshot_at:
                continue
            key = _snapshot_bucket(r.snapshot_at)
            item = buckets.setdefault(
                key,
                {"snapshot_at": key, "equity": 0.0, "balance": 0.0, "unrealized_pnl": 0.0},
            )
            item["equity"] += float(r.equity or 0)
            item["balance"] += float(r.balance or r.equity or 0)
            item["unrealized_pnl"] += float(r.unrealized_pnl or 0)
        return [buckets[k] for k in sorted(buckets)]

    return [
        {
            "snapshot_at": r.snapshot_at,
            "equity": float(r.equity or 0),
            "balance": float(r.balance or r.equity or 0),
            "unrealized_pnl": float(r.unrealized_pnl or 0),
        }
        for r in rows
    ]


async def equity_curve(
    db: AsyncSession,
    customer_id: int,
    exchange: str | None = None,
    days: int = 30,
    exchange_account_id: int | None = None,
) -> list[dict]:
    rows = await _snapshot_points(db, customer_id, exchange, days, exchange_account_id)
    return [
        {
            "snapshot_at": r["snapshot_at"].isoformat() if r.get("snapshot_at") else None,
            "equity": round(float(r.get("equity") or 0), 2),
            "balance": round(float(r.get("balance") or 0), 2),
            "unrealized_pnl": round(float(r.get("unrealized_pnl") or 0), 2),
        }
        for r in rows
    ]




async def calculate_advanced_metrics(
    db: AsyncSession,
    customer_id: int,
    exchange_account_id: int | None = None,
) -> dict:
    """计算高级指标:余额、最大回撤、月化收益率、年化收益率、盈亏比、夏普比。"""
    import math
    from sqlalchemy import func as sql_func
    from datetime import datetime, timedelta, timezone

    # 1. 最大回撤 + 余额 (基于净值快照)
    since_90d = datetime.now(timezone.utc) - timedelta(days=90)
    snapshots = await _snapshot_points(db, customer_id, days=90, exchange_account_id=exchange_account_id)

    max_drawdown = 0.0
    peak = 0.0
    for snap in snapshots:
        eq = float(snap.get("equity") or 0)
        if eq > peak:
            peak = eq
        if peak > 0:
            dd = (peak - eq) / peak
            if dd > max_drawdown:
                max_drawdown = dd

    # 最新余额(取最新一条快照的 balance)
    balance = 0.0
    if snapshots:
        balance = float(snapshots[-1].get("balance") or snapshots[-1].get("equity") or 0)

    # 2. 月化/年化收益率 + 夏普比 (基于净值快照)
    monthly_return = 0.0
    annual_return = 0.0
    sharpe_ratio = 0.0
    if len(snapshots) >= 2:
        first_eq = float(snapshots[0].get("equity") or 0)
        last_eq = float(snapshots[-1].get("equity") or 0)
        if first_eq > 0:
            total_return = (last_eq - first_eq) / first_eq
            elapsed_seconds = (snapshots[-1]["snapshot_at"] - snapshots[0]["snapshot_at"]).total_seconds()
            days = elapsed_seconds / 86400 if elapsed_seconds > 0 else 0
            if days >= 7:
                daily_return = total_return / days
                monthly_return = ((1 + daily_return) ** 30 - 1) * 100
                monthly_return = max(-100, min(999, monthly_return))
                annual_return = ((1 + daily_return) ** 365 - 1) * 100
                annual_return = max(-100, min(999, annual_return))
            else:
                # 样本不足 7 天时不做夸张年化，直接展示当前样本期收益率。
                monthly_return = total_return * 100
                annual_return = total_return * 100

        # 夏普比: 基于日收益率,年化因子=sqrt(365)
        # S41修正: 旧实现用5分钟快照年化(sqrt(105120)),数值虚高
        # 改为按日聚合,至少7天数据才计算
        _daily_eq = {}
        for snap in snapshots:
            _dk = snap["snapshot_at"].strftime("%Y-%m-%d")
            _daily_eq[_dk] = float(snap.get("equity") or 0)
        _sorted_days = sorted(_daily_eq.keys())
        if len(_sorted_days) >= 7:
            daily_returns = []
            for i in range(1, len(_sorted_days)):
                _prev = _daily_eq[_sorted_days[i - 1]]
                _curr = _daily_eq[_sorted_days[i]]
                if _prev > 0:
                    daily_returns.append((_curr - _prev) / _prev)
            if len(daily_returns) >= 2:
                avg_ret = sum(daily_returns) / len(daily_returns)
                variance = sum((r - avg_ret) ** 2 for r in daily_returns) / (len(daily_returns) - 1)
                std_ret = math.sqrt(variance) if variance > 0 else 0
                if std_ret > 0:
                    sharpe_ratio = round((avg_ret / std_ret) * math.sqrt(365), 2)

        # 3. 盈亏比 (平均盈利 / 平均亏损)
    win_pnl = (
        await db.execute(
            select(sql_func.avg(Trade.realized_pnl)).where(
                Trade.customer_id == customer_id,
                Trade.is_close.is_(True),
                (Trade.exchange_account_id == exchange_account_id) if exchange_account_id else True,
                Trade.realized_pnl > 0,
            )
        )
    ).scalar_one()
    loss_pnl = (
        await db.execute(
            select(sql_func.avg(Trade.realized_pnl)).where(
                Trade.customer_id == customer_id,
                Trade.is_close.is_(True),
                (Trade.exchange_account_id == exchange_account_id) if exchange_account_id else True,
                Trade.realized_pnl < 0,
            )
        )
    ).scalar_one()

    win_pnl = float(win_pnl or 0)
    loss_pnl = abs(float(loss_pnl or 0))
    profit_loss_ratio = round(win_pnl / loss_pnl, 2) if loss_pnl > 0 else 0.0

    # S41: compute unrealized P&L from open positions
    unrealized_pnl = 0.0
    try:
        from app.services.position_manager import _get_cached_price, compute_pnl
        _open_stmt = select(Position).where(
            Position.customer_id == customer_id,
            Position.status == "open",
            Position.parent_id.is_not(None),
        )
        if exchange_account_id:
            _open_stmt = _open_stmt.where(Position.exchange_account_id == exchange_account_id)
        for pos in (await db.execute(_open_stmt)).scalars():
            try:
                _cp = await _get_cached_price(db, pos.symbol, pos.exchange)
                _pnl, _ = compute_pnl(pos, _cp)
                unrealized_pnl += _pnl
            except Exception:
                pass
    except Exception:
        pass

    return {
        "balance": round(balance, 2),
        "unrealized_pnl": round(unrealized_pnl, 2),
        "max_drawdown": round(max_drawdown * 100, 2),
        "monthly_return": round(monthly_return, 2),
        "annual_return": round(annual_return, 2),
        "profit_loss_ratio": profit_loss_ratio,
        "sharpe_ratio": sharpe_ratio,
    }


async def dashboard_stats(db: AsyncSession, customer_id: int, exchange_account_id: int | None = None) -> dict:
    """仪表盘关键指标。

    胜率和交易次数按 Position 统计(每个子仓位从开到平算 1 次),避免分批止盈重复计数。
    利润按 Trade 统计(按成交时间累加,准确反映当日/累计盈亏)。
    """
    today = datetime.now(timezone.utc).replace(hour=0, minute=0, second=0, microsecond=0)
    account_filter_pos = (Position.exchange_account_id == exchange_account_id) if exchange_account_id else True
    account_filter_trade = (Trade.exchange_account_id == exchange_account_id) if exchange_account_id else True
    # Open positions: child positions only, same as /positions.
    open_positions = (
        await db.execute(
            select(func.count(Position.id)).where(
                Position.customer_id == customer_id,
                Position.status == "open",
                Position.parent_id.is_not(None),
                account_filter_pos,
            )
        )
    ).scalar_one()
    today_pnl = (
        await db.execute(
            select(func.coalesce(func.sum(Trade.realized_pnl), 0.0)).where(
                Trade.customer_id == customer_id,
                Trade.is_close.is_(True),
                Trade.executed_at >= today,
                account_filter_trade,
            )
        )
    ).scalar_one()
    total_pnl = (
        await db.execute(
            select(func.coalesce(func.sum(Trade.realized_pnl), 0.0)).where(
                Trade.customer_id == customer_id,
                Trade.is_close.is_(True),
                account_filter_trade,
            )
        )
    ).scalar_one()
    total_fee = (
        await db.execute(
            select(func.coalesce(func.sum(Trade.fee), 0.0)).where(
                Trade.customer_id == customer_id,
                account_filter_trade,
            )
        )
    ).scalar_one()
    total_trades = (
        await db.execute(
            select(func.count(Position.id)).where(
                Position.customer_id == customer_id,
                Position.status == "closed",
                Position.parent_id.is_not(None),
                account_filter_pos,
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
                account_filter_pos,
            )
        )
    ).scalar_one()
    win_rate = (win_trades / total_trades * 100) if total_trades else 0
    # 高级指标
    advanced = await calculate_advanced_metrics(db, customer_id, exchange_account_id)
    return {
        "open_positions": open_positions,
        "today_pnl": round(float(today_pnl), 2),
        "total_pnl": round(float(total_pnl), 2),
        "total_fee": round(float(total_fee), 4),
        "total_trades": total_trades,
        "win_rate": round(win_rate, 2),
        **advanced,
    }


async def customer_profit_stats(
    db: AsyncSession,
    start_date: datetime,
    end_date: datetime,
    customer_type: str | None = None,
) -> list[dict]:
    """按时间范围统计每个客户的利润、手续费、邀请佣金。

    Args:
        db: 异步数据库会话
        start_date: 开始时间(带时区),按 Trade.executed_at 过滤
        end_date: 结束时间(带时区)
        customer_type: 客户分类过滤 normal|internal,None 则全部

    Returns:
        list[dict],每个客户一条,字段:
          - customer_id, username, display_name, customer_type
          - total_pnl (净盈亏,Trade.realized_pnl 之和,is_close=True)
          - total_fee (手续费,Trade.fee 之和)
          - trade_count (平仓交易数)
          - commission_earned (邀请佣金收入,ReferralCommission.commission_amount 之和)
          - invited_count (邀请人数)
          - inviter_name (邀请人用户名,无则空字符串)
    """
    from loguru import logger

    from app.models.customer import Customer
    from app.models.referral import ReferralCommission

    # 1. 查询所有客户(按 customer_type 过滤),构建基础信息与邀请人映射
    cust_stmt = select(Customer)
    if customer_type:
        cust_stmt = cust_stmt.where(Customer.customer_type == customer_type)
    customers = (await db.execute(cust_stmt.order_by(Customer.id))).scalars().all()
    if not customers:
        return []

    cust_by_id = {c.id: c for c in customers}
    customer_ids = list(cust_by_id.keys())

    # 邀请人ID -> 邀请人用户名(邀请人可能不在当前 customer_type 过滤范围内,单独查)
    inviter_ids = {c.invited_by for c in customers if c.invited_by is not None}
    inviter_name_map: dict[int, str] = {}
    if inviter_ids:
        inviter_rows = (
            await db.execute(select(Customer.id, Customer.username).where(Customer.id.in_(inviter_ids)))
        ).all()
        inviter_name_map = {r.id: r.username for r in inviter_rows}

    # 2. 平仓利润 + 手续费 + 平仓交易数 (按 Trade,is_close=True,时间范围过滤)
    pnl_stmt = (
        select(
            Trade.customer_id,
            func.coalesce(func.sum(Trade.realized_pnl), 0.0).label("total_pnl"),
            func.coalesce(func.sum(Trade.fee), 0.0).label("total_fee"),
            func.count(Trade.id).label("trade_count"),
        )
        .where(
            Trade.customer_id.in_(customer_ids),
            Trade.is_close.is_(True),
            Trade.executed_at >= start_date,
            Trade.executed_at <= end_date,
        )
        .group_by(Trade.customer_id)
    )
    pnl_rows = {
        r.customer_id: r for r in (await db.execute(pnl_stmt)).all()
    }

    # 3. 邀请佣金收入 (ReferralCommission.commission_amount 之和,inviter_id = 客户ID)
    #    按佣金产生时间 created_at 过滤(与交易时间范围对齐)
    comm_stmt = (
        select(
            ReferralCommission.inviter_id,
            func.coalesce(func.sum(ReferralCommission.commission_amount), 0.0).label("commission_earned"),
        )
        .where(
            ReferralCommission.inviter_id.in_(customer_ids),
            ReferralCommission.created_at >= start_date,
            ReferralCommission.created_at <= end_date,
        )
        .group_by(ReferralCommission.inviter_id)
    )
    comm_rows = {
        r.inviter_id: float(r.commission_earned)
        for r in (await db.execute(comm_stmt)).all()
    }

    # 4. 邀请人数 (每个客户邀请了几个客户,统计 customers 表中 invited_by = 客户ID)
    invite_count_stmt = (
        select(
            Customer.invited_by,
            func.count(Customer.id).label("invited_count"),
        )
        .where(Customer.invited_by.in_(customer_ids))
        .group_by(Customer.invited_by)
    )
    invite_count_rows = {
        r.invited_by: r.invited_count
        for r in (await db.execute(invite_count_stmt)).all()
    }

    # 5. 合并结果
    result: list[dict] = []
    for c in customers:
        pnl_row = pnl_rows.get(c.id)
        total_pnl = float(pnl_row.total_pnl) if pnl_row else 0.0
        total_fee = float(pnl_row.total_fee) if pnl_row else 0.0
        trade_count = int(pnl_row.trade_count) if pnl_row else 0
        result.append({
            "customer_id": c.id,
            "username": c.username,
            "display_name": c.display_name or "",
            "customer_type": c.customer_type or "normal",
            "total_pnl": round(total_pnl, 4),
            "total_fee": round(total_fee, 4),
            "trade_count": trade_count,
            "commission_earned": round(comm_rows.get(c.id, 0.0), 4),
            "invited_count": invite_count_rows.get(c.id, 0),
            "inviter_name": inviter_name_map.get(c.invited_by, "") if c.invited_by else "",
        })

    logger.debug(
        f"customer_profit_stats: range={start_date}~{end_date} "
        f"type={customer_type} customers={len(result)}"
    )
    return result


_BEIJING_TZ = timezone(timedelta(hours=8))


def _beijing_day_range(day=None) -> tuple[datetime.date, datetime, datetime]:
    """返回北京时间自然日及对应 UTC 起止时间。"""
    if day is None:
        local_day = datetime.now(_BEIJING_TZ).date()
    elif isinstance(day, str):
        local_day = datetime.strptime(day, "%Y-%m-%d").date()
    else:
        local_day = day
    start_local = datetime.combine(local_day, datetime.min.time(), tzinfo=_BEIJING_TZ)
    end_local = start_local + timedelta(days=1)
    return local_day, start_local.astimezone(timezone.utc), end_local.astimezone(timezone.utc)


async def take_daily_risk_snapshot(
    db: AsyncSession,
    customer_id: int,
    exchange: str = "all",
    day=None,
    testnet: bool | None = None,
) -> dict:
    """生成或更新当天日风控快照。

    口径:
      - day 使用北京时间自然日。
      - realized_pnl 来自当天已平仓 Trade。
      - unrealized_pnl 来自当前 open 仓位按最新价格估算。
      - base_equity 优先取当日首条权益快照,否则取最近权益快照。
    """
    from loguru import logger
    from sqlalchemy.dialects.postgresql import insert

    from app.models.config import DailyRiskSnapshot, EquitySnapshot, RiskConfig
    from app.services import exchange_adapter
    from app.services.position_manager import compute_pnl

    local_day, start_utc, end_utc = _beijing_day_range(day)

    trade_row = (
        await db.execute(
            select(
                func.coalesce(func.sum(Trade.realized_pnl), 0.0).label("realized_pnl"),
                func.count(Trade.id).label("trade_count"),
                func.count(Trade.id).filter(Trade.is_close.is_(True)).label("close_count"),
            ).where(
                Trade.customer_id == customer_id,
                Trade.executed_at >= start_utc,
                Trade.executed_at < end_utc,
                (Trade.exchange == exchange) if exchange != "all" else True,
            )
        )
    ).one()
    realized_pnl = float(trade_row.realized_pnl or 0.0)
    trade_count = int(trade_row.trade_count or 0)
    close_count = int(trade_row.close_count or 0)

    open_stmt = select(Position).where(
        Position.customer_id == customer_id,
        Position.status == "open",
        Position.parent_id.is_(None),
    )
    if exchange != "all":
        open_stmt = open_stmt.where(Position.exchange == exchange)
    open_positions_rows = (await db.execute(open_stmt)).scalars().all()
    open_positions = len(open_positions_rows)

    unrealized_pnl = 0.0
    if open_positions_rows:
        symbols_by_exchange: dict[str, set[str]] = {}
        for p in open_positions_rows:
            symbols_by_exchange.setdefault(p.exchange, set()).add(p.symbol)
        price_cache: dict[tuple[str, str], float] = {}
        for ex_name, symbols in symbols_by_exchange.items():
            try:
                batch = await exchange_adapter.fetch_market_prices_batch(ex_name, list(symbols))
                for sym, price in batch.items():
                    price_cache[(ex_name, sym)] = price
            except Exception as e:
                logger.warning(f"获取 {ex_name} 市场价格失败,跳过: {e}")
                continue
        for p in open_positions_rows:
            price = price_cache.get((p.exchange, p.symbol), 0.0)
            if price:
                pnl, _ = compute_pnl(p, price)
                unrealized_pnl += pnl

    total_daily_pnl = realized_pnl + unrealized_pnl

    latest_snapshot_stmt = (
        select(EquitySnapshot)
        .where(EquitySnapshot.customer_id == customer_id)
        .order_by(EquitySnapshot.snapshot_at.desc())
        .limit(1)
    )
    first_day_snapshot_stmt = (
        select(EquitySnapshot)
        .where(
            EquitySnapshot.customer_id == customer_id,
            EquitySnapshot.snapshot_at >= start_utc,
            EquitySnapshot.snapshot_at < end_utc,
        )
        .order_by(EquitySnapshot.snapshot_at.asc())
        .limit(1)
    )
    if exchange != "all":
        latest_snapshot_stmt = latest_snapshot_stmt.where(EquitySnapshot.exchange == exchange)
        first_day_snapshot_stmt = first_day_snapshot_stmt.where(EquitySnapshot.exchange == exchange)

    latest_snapshot = (await db.execute(latest_snapshot_stmt)).scalar_one_or_none()
    first_day_snapshot = (await db.execute(first_day_snapshot_stmt)).scalar_one_or_none()
    equity = float((latest_snapshot.equity if latest_snapshot else 0.0) or 0.0)
    balance = float((latest_snapshot.balance if latest_snapshot else 0.0) or 0.0)
    base_equity = float(
        (first_day_snapshot.equity if first_day_snapshot else None)
        or (latest_snapshot.equity if latest_snapshot else 0.0)
        or 0.0
    )

    cfg = (
        await db.execute(
            select(RiskConfig)
            .where(
                RiskConfig.customer_id == customer_id,
                RiskConfig.enabled.is_(True),
                RiskConfig.exchange.in_([exchange, "all"]),
            )
            .order_by((RiskConfig.exchange == exchange).desc())
            .limit(1)
        )
    ).scalar_one_or_none()
    max_daily_loss_pct = float((cfg.max_daily_loss_pct if cfg else 0.0) or 0.0)
    loss_pct = (abs(total_daily_pnl) / base_equity * 100.0) if total_daily_pnl < 0 and base_equity > 0 else 0.0
    risk_triggered = bool(max_daily_loss_pct > 0 and loss_pct >= max_daily_loss_pct)
    if risk_triggered:
        risk_level = "triggered"
    elif max_daily_loss_pct > 0 and loss_pct >= max_daily_loss_pct * 0.8:
        risk_level = "danger"
    elif max_daily_loss_pct > 0 and loss_pct >= max_daily_loss_pct * 0.5:
        risk_level = "warning"
    else:
        risk_level = "normal"

    now = datetime.now(timezone.utc)
    values = {
        "customer_id": customer_id,
        "exchange": exchange,
        "day": local_day,
        "realized_pnl": realized_pnl,
        "unrealized_pnl": unrealized_pnl,
        "total_daily_pnl": total_daily_pnl,
        "equity": equity,
        "balance": balance,
        "base_equity": base_equity,
        "max_daily_loss_pct": max_daily_loss_pct,
        "loss_pct": loss_pct,
        "risk_level": risk_level,
        "risk_triggered": risk_triggered,
        "open_positions": open_positions,
        "trade_count": trade_count,
        "close_count": close_count,
        "snapshot_at": now,
        "updated_at": now,
    }
    stmt = insert(DailyRiskSnapshot).values(**values)
    stmt = stmt.on_conflict_do_update(
        index_elements=["customer_id", "exchange", "day"],
        set_={k: v for k, v in values.items() if k not in ("customer_id", "exchange", "day")},
    )
    # M11修复: 添加try/except/rollback,防止upsert失败导致session脏状态
    try:
        await db.execute(stmt)
        try:
            await db.commit()
        except Exception:
            await db.rollback()
            logger.exception("db commit failed")
            raise
    except Exception as e:
        await db.rollback()
        logger.error(f"日风控快照写入失败 customer={customer_id} day={local_day}: {e}")
        raise
    return {
        **values,
        "day": local_day.isoformat(),
        "snapshot_at": now.isoformat(),
    }


async def daily_risk_snapshots(
    db: AsyncSession,
    customer_id: int,
    start_day,
    end_day,
    exchange: str | None = None,
) -> list[dict]:
    """读取指定日期范围内每日最新风控快照。"""
    from app.models.config import DailyRiskSnapshot

    stmt = select(DailyRiskSnapshot).where(
        DailyRiskSnapshot.customer_id == customer_id,
        DailyRiskSnapshot.day >= start_day,
        DailyRiskSnapshot.day < end_day,
    )
    if exchange:
        stmt = stmt.where(DailyRiskSnapshot.exchange == exchange)
    stmt = stmt.order_by(DailyRiskSnapshot.day.asc(), DailyRiskSnapshot.exchange.asc())
    rows = (await db.execute(stmt)).scalars().all()
    return [
        {
            "day": r.day.isoformat() if r.day else None,
            "exchange": r.exchange,
            "realized_pnl": round(float(r.realized_pnl or 0), 8),
            "unrealized_pnl": round(float(r.unrealized_pnl or 0), 8),
            "total_daily_pnl": round(float(r.total_daily_pnl or 0), 8),
            "equity": round(float(r.equity or 0), 8),
            "balance": round(float(r.balance or 0), 8),
            "base_equity": round(float(r.base_equity or 0), 8),
            "max_daily_loss_pct": round(float(r.max_daily_loss_pct or 0), 4),
            "loss_pct": round(float(r.loss_pct or 0), 4),
            "risk_level": r.risk_level,
            "risk_triggered": bool(r.risk_triggered),
            "open_positions": int(r.open_positions or 0),
            "trade_count": int(r.trade_count or 0),
            "close_count": int(r.close_count or 0),
            "snapshot_at": r.snapshot_at.isoformat() if r.snapshot_at else None,
        }
        for r in rows
    ]
