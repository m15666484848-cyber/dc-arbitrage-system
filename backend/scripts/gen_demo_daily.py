"""dclh001 每日演示数据增量生成器(幂等,由宿主机 cron 每 5 分钟调用)。

- 每天北京时间 0 点后首次运行为当天生成交易计划(以日期做随机种子,重跑结果一致)
- 回合到达平仓时间后才落库(主/子仓位+开平两条成交),当日数据随时间逐步"出现"
- 净值快照按 5 分钟桶增量补齐: 余额=基础资金+累计已平盈亏,在途回合按进度给浮盈亏
- 某日已存在平仓成交则整日跳过(防重复,兼容回填/手工数据)
- 活动窗口限制在北京时间 09:00-23:45,保证仪表盘"今日盈亏"(UTC 日界=北京 08:00)
  与统计日历(北京日)口径一致
- 客户 20 不存在/改名/停用时静默退出
- 服务器中断自动补齐: 最近 5 天内空缺日期的计划会在恢复后一次性落库
"""
import sys
sys.path.insert(0, "/app")

import asyncio
import random
from datetime import datetime, timedelta, timezone

from sqlalchemy import text
from app.core.database import AsyncSessionLocal
from app.models.trading import Position, Trade
from app.models.config import EquitySnapshot

CID = 20
USERNAME = "dclh001"
BASE = 30000.0
EXCHANGE = "bybit"
BJ = timezone(timedelta(hours=8))
UTC = timezone.utc
CATCHUP_DAYS = 5

SYMBOLS = {
    "BTC/USDT": dict(p0=77000.0, dp=3),
    "ETH/USDT": dict(p0=2400.0, dp=2),
    "SOL/USDT": dict(p0=150.0, dp=1),
    "XRP/USDT": dict(p0=2.10, dp=0),
    "DOGE/USDT": dict(p0=0.160, dp=0),
    "BNB/USDT": dict(p0=650.0, dp=2),
    "LINK/USDT": dict(p0=16.0, dp=1),
    "AVAX/USDT": dict(p0=25.0, dp=1),
}


def day_plan(day_str: str) -> list[dict]:
    """某日完整交易计划(确定性: 同一天每次生成完全一致)。"""
    rng = random.Random(f"dclh-v2-{day_str}")
    if rng.random() < 0.25:
        target = -rng.uniform(40, 130)
    else:
        target = rng.uniform(35, 290)

    if target >= 0:
        n = rng.choice([2, 3, 3, 4])
        nw = max(1, int(round(n * rng.uniform(0.75, 1.0))))
        parts = [rng.uniform(45, 215) for _ in range(nw)]
        parts += [-rng.uniform(15, 65) for _ in range(n - nw)]
    else:
        n = rng.choice([3, 3, 4])
        nw = 0 if rng.random() < 0.5 else 1
        parts = [rng.uniform(45, 160) for _ in range(nw)]
        parts += [-rng.uniform(25, 115) for _ in range(n - nw)]

    for _ in range(80):
        diff = target - sum(parts)
        if abs(diff) < 0.05:
            break
        if diff > 0:
            cands = [i for i, v in enumerate(parts) if v < 315]
            if not cands:
                break
            parts[rng.choice(cands)] += min(diff, rng.uniform(5, 40))
        else:
            cands = [i for i, v in enumerate(parts) if v > -185]
            if not cands:
                break
            parts[rng.choice(cands)] -= min(-diff, rng.uniform(5, 40))
    resid = target - sum(parts)
    if abs(resid) >= 0.005:
        parts[-1] += resid
    merged = []
    for v in parts:
        if abs(v) < 10 and merged:
            same = [m for m in merged if (m > 0) == (v > 0)]
            if same:
                same[0] += v
                continue
        merged.append(v)
    parts = merged or parts

    y, m, d = map(int, day_str.split("-"))
    day_start = datetime(y, m, d, tzinfo=BJ)
    lo = day_start + timedelta(hours=9)
    hi = day_start + timedelta(hours=21)
    cap = day_start + timedelta(hours=23, minutes=45)
    n = max(1, len(parts))
    span = (hi - lo).total_seconds()
    opens = []
    for i in range(n):
        f0, f1 = i / n, (i + 1) / n
        opens.append(lo + timedelta(seconds=span * (f0 + rng.uniform(0.1, 0.9) * (f1 - f0))))
    opens.sort()

    rounds = []
    for idx, open_t in enumerate(opens):
        close_t = open_t + timedelta(minutes=rng.uniform(25, 360))
        if close_t > cap:
            close_t = cap - timedelta(minutes=rng.uniform(1, 9))
        if close_t <= open_t:
            close_t = open_t + timedelta(minutes=3)
        rounds.append(dict(
            pnl=round(parts[idx], 2),
            open=open_t.replace(microsecond=0),
            close=close_t.replace(microsecond=0),
        ))
    resid = round(target - sum(r["pnl"] for r in rounds), 2)
    if abs(resid) >= 0.01 and rounds:
        rounds[-1]["pnl"] = round(rounds[-1]["pnl"] + resid, 2)

    for r in rounds:
        sym = rng.choice(list(SYMBOLS.keys()))
        cfg = SYMBOLS[sym]
        entry = cfg["p0"] * (1 + rng.gauss(0, 0.055))
        side = rng.choice(["long", "short"])
        pnl = r["pnl"]
        pct = rng.uniform(0.015, 0.06)
        notional = min(6000.0, max(800.0, abs(pnl) / pct))
        qty = round(notional / entry, cfg["dp"])
        if qty <= 0:
            qty = 1
        notional = qty * entry
        real_pct = pnl / notional if notional else 0.0
        exit_p = entry * (1 + real_pct) if side == "long" else entry * (1 - real_pct)
        if exit_p <= 0:
            exit_p = entry * (1 - real_pct / 2)
        open_fee = round(notional * 0.00055 * rng.uniform(0.9, 1.15), 6)
        close_fee = round(qty * exit_p * 0.00055 * rng.uniform(0.9, 1.15), 6)
        if side == "long":
            tp = [entry * 1.012, entry * 1.022, entry * 1.035]
            sl = entry * 0.988
        else:
            tp = [entry * 0.988, entry * 0.978, entry * 0.965]
            sl = entry * 1.012
        r.update(
            symbol=sym, side=side, entry=round(entry, 6), exit=round(exit_p, 6),
            qty=qty,
            tp_levels=[
                {"level": i + 1, "price": round(p, 6), "pct": 0.3333, "status": "pending"}
                for i, p in enumerate(tp)
            ],
            sl=round(sl, 6), leverage=rng.choice([5, 10, 10, 20]),
            open_fee=open_fee, close_fee=close_fee,
        )
    return rounds


def make_position(r: dict, parent_id: int | None) -> Position:
    return Position(
        customer_id=CID, kol_id=None, parent_id=parent_id,
        exchange=EXCHANGE, symbol=r["symbol"], side=r["side"],
        entry_price=r["entry"], qty=0.0, initial_qty=r["qty"],
        tp_levels=r["tp_levels"], sl=r["sl"], initial_sl=r["sl"],
        leverage=r["leverage"], cost_protection=False, breakeven_moved=False,
        trailing_stop=False, trailing_callback=0.0,
        status="closed", realized_pnl=r["pnl"],
        opened_at=r["open"], closed_at=r["close"],
        entry_fee=r["open_fee"], batch_no=1, exchange_account_id=None,
        tp_sl_source="kol", source="direct",
        exchange_stop_order_id="", exchange_stop_qty=0.0, exchange_stop_price=0.0,
    )


async def insert_round(db, r: dict):
    main_pos = make_position(r, None)
    db.add(main_pos)
    await db.flush()
    child = make_position(r, main_pos.id)
    db.add(child)
    await db.flush()
    open_side = "buy" if r["side"] == "long" else "sell"
    close_side = "sell" if r["side"] == "long" else "buy"
    db.add(Trade(
        customer_id=CID, kol_id=None, position_id=child.id, order_id=None,
        exchange=EXCHANGE, symbol=r["symbol"], side=open_side,
        qty=r["qty"], price=r["entry"], fee=r["open_fee"],
        realized_pnl=0.0, is_close=False, tp_level=0,
        executed_at=r["open"], exchange_account_id=None,
    ))
    db.add(Trade(
        customer_id=CID, kol_id=None, position_id=child.id, order_id=None,
        exchange=EXCHANGE, symbol=r["symbol"], side=close_side,
        qty=r["qty"], price=r["exit"], fee=r["close_fee"],
        realized_pnl=r["pnl"], is_close=True, tp_level=-1,
        executed_at=r["close"], exchange_account_id=None,
    ))


async def fill_trades(db, now: datetime, now_bj: datetime) -> int:
    inserted = 0
    for offset in range(CATCHUP_DAYS - 1, -1, -1):
        day = now_bj.date() - timedelta(days=offset)
        day_str = day.isoformat()
        day_start_utc = datetime(day.year, day.month, day.day, tzinfo=BJ).astimezone(UTC)
        day_end_utc = day_start_utc + timedelta(days=1)
        n_exist = (await db.execute(text(
            "SELECT count(*) FROM trades WHERE customer_id = :c AND is_close "
            "AND executed_at >= :s AND executed_at < :e"
        ), {"c": CID, "s": day_start_utc, "e": day_end_utc})).scalar()
        if n_exist:
            continue
        for r in day_plan(day_str):
            if r["close"] <= now:
                await insert_round(db, r)
                inserted += 1
    if inserted:
        await db.commit()
    return inserted


async def fill_snapshots(db, now: datetime, now_bj: datetime) -> int:
    last = (await db.execute(text(
        "SELECT max(snapshot_at) FROM equity_snapshots WHERE customer_id = :c"
    ), {"c": CID})).scalar()
    t_end = now.replace(minute=(now.minute // 5) * 5, second=0, microsecond=0)
    if last is None:
        last = t_end - timedelta(days=30)
    t = last + timedelta(minutes=5)
    if t > t_end:
        return 0

    closed = (await db.execute(text(
        "SELECT executed_at, realized_pnl FROM trades "
        "WHERE customer_id = :c AND is_close ORDER BY executed_at"
    ), {"c": CID})).all()
    plans = [day_plan((now_bj.date() - timedelta(days=o)).isoformat()) for o in (1, 0)]

    snaps = []
    ci = 0
    bal = BASE
    while t <= t_end:
        while ci < len(closed) and closed[ci][0] <= t:
            bal += float(closed[ci][1])
            ci += 1
        unr = 0.0
        for plan in plans:
            for r in plan:
                if r["open"] <= t < r["close"]:
                    dur = max(1.0, (r["close"] - r["open"]).total_seconds())
                    prog = min(1.0, max(0.0, (t - r["open"]).total_seconds() / dur))
                    mid = 4 * prog * (1 - prog)
                    noise = random.gauss(0, max(2.0, 0.3 * abs(r["pnl"]))) * mid
                    unr += r["pnl"] * prog + noise
        snaps.append(EquitySnapshot(
            customer_id=CID, exchange=EXCHANGE,
            equity=round(bal + unr, 4), balance=round(bal, 4),
            unrealized_pnl=round(unr, 4),
            snapshot_at=t, exchange_account_id=None,
        ))
        t += timedelta(minutes=5)
    if snaps:
        db.add_all(snaps)
        await db.commit()
    return len(snaps)


async def main():
    now = datetime.now(UTC)
    now_bj = now.astimezone(BJ)
    async with AsyncSessionLocal() as db:
        row = (await db.execute(
            text("SELECT username, is_active FROM customers WHERE id = :i"), {"i": CID}
        )).one_or_none()
        if not row or row.username != USERNAME or not row.is_active:
            return
        n_rounds = await fill_trades(db, now, now_bj)
        n_snaps = await fill_snapshots(db, now, now_bj)
        if n_rounds or n_snaps:
            print(f"{now_bj:%Y-%m-%d %H:%M} rounds=+{n_rounds} snapshots=+{n_snaps}")


if __name__ == "__main__":
    asyncio.run(main())
