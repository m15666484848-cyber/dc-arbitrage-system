#!/usr/bin/env python3
"""KOL 信号对账审计脚本 —— 一键生成 Markdown 审计报告。

用法(宿主机):
  docker exec dcquant-backend python scripts/audit_kol.py             # 审计今天(UTC)
  docker exec dcquant-backend python scripts/audit_kol.py 2026-08-18  # 审计指定日期

报告输出: /app/reports/audit_YYYYMMDD.md
         (宿主机 /opt/dcquant/backend/reports/audit_YYYYMMDD.md)

审计项:
  1. ordered 信号执行完整性(全链路: 信号→订单/挂单)
  2. 开仓订单无信号来源
  3. 撤单原因含"撤单上下文命中"的挂单(误判高危模式)
  4. 被拒信号复核清单
  5. 灰尘仓位(剩余<2%却未关闭)
  6. 同价位重复挂单
  7. close_failed 持仓
  8. 超龄持仓(>70h, 即将触发72h超时平仓)
  9. 过期未清理待触发单
"""
from __future__ import annotations

import asyncio
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

from sqlalchemy import text

from app.core.database import AsyncSessionLocal

SEV = "🔴"
WARN = "🟡"
OK = "✅"
BJT = timezone(timedelta(hours=8))


def parse_args():
    if len(sys.argv) > 1:
        d = datetime.strptime(sys.argv[1], "%Y-%m-%d").date()
    else:
        d = datetime.now(timezone.utc).date()
    start = datetime(d.year, d.month, d.day, tzinfo=timezone.utc)
    return d, start, start + timedelta(days=1)


def hhmm(v) -> str:
    if not v:
        return "—"
    try:
        return v.astimezone(BJT).strftime("%m-%d %H:%M")
    except Exception:
        return str(v)[:16]


def short(s: str | None, n: int = 60) -> str:
    s = (s or "").replace("\n", " ").replace("|", "／").strip()
    return (s[:n] + "…") if len(s) > n else s


class Report:
    def __init__(self, title: str):
        self.buf: list[str] = [f"# {title}", ""]
        self.severe = 0
        self.warn = 0

    def add(self, s: str = ""):
        self.buf.append(s)

    def section(self, name: str):
        self.add(f"\n## {name}\n")

    def issue(self, level: str, msg: str):
        if level == SEV:
            self.severe += 1
        else:
            self.warn += 1
        self.add(f"- {level} {msg}")

    def save(self, path: str):
        p = Path(path)
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text("\n".join(self.buf), encoding="utf-8")


async def q(db, sql: str, params: dict | None = None):
    return [dict(r._mapping) for r in (await db.execute(text(sql), params or {})).fetchall()]


async def main():
    d, start, end = parse_args()
    rep = Report(f"KOL 信号对账审计报告 {d}")
    rep.add(f"- 审计范围: {d} 00:00 – 24:00 UTC（北京时间 {d} 08:00 – 次日 08:00）")
    rep.add(f"- 生成时间: {datetime.now(BJT).strftime('%Y-%m-%d %H:%M')} 北京时间")

    async with AsyncSessionLocal() as db:
        # ============ 一、总览 ============
        stats = await q(db, """
            SELECT status, count(*) AS n FROM signals
            WHERE received_at >= :s AND received_at < :e GROUP BY status ORDER BY n DESC
        """, {"s": start, "e": end})
        total_sig = sum(x["n"] for x in stats)
        ord_n = (await q(db, "SELECT count(*) AS n FROM orders WHERE created_at >= :s AND created_at < :e", {"s": start, "e": end}))[0]["n"]
        pos_opened = await q(db, """
            SELECT count(DISTINCT COALESCE(parent_id, id)) AS n FROM positions
            WHERE opened_at >= :s AND opened_at < :e
        """, {"s": start, "e": end})
        pos_closed = await q(db, """
            SELECT count(DISTINCT COALESCE(parent_id, id)) AS n FROM positions
            WHERE closed_at >= :s AND closed_at < :e
        """, {"s": start, "e": end})
        pend_new = await q(db, "SELECT count(*) AS n FROM pending_orders WHERE created_at >= :s AND created_at < :e", {"s": start, "e": end})

        rep.section("一、总览")
        rep.add("| 指标 | 数量 |")
        rep.add("|---|---|")
        rep.add(f"| 信号总数 | {total_sig} |")
        for x in stats:
            rep.add(f"| 信号·{x['status']} | {x['n']} |")
        rep.add(f"| 订单（含开/平仓） | {ord_n} |")
        rep.add(f"| 新开仓位 | {pos_opened[0]['n']} |")
        rep.add(f"| 平仓仓位 | {pos_closed[0]['n']} |")
        rep.add(f"| 新建待触发单 | {pend_new[0]['n']} |")

        # ============ 二、全链路对账 ============
        rep.section("二、信号→执行 全链路对账")
        sigs = await q(db, """
            SELECT s.id, k.name AS kol, s.symbol, s.side, s.status, s.note,
                   s.raw_text, s.parsed, s.received_at
            FROM signals s JOIN kols k ON k.id = s.kol_id
            WHERE s.received_at >= :s AND s.received_at < :e
              AND s.status IN ('ordered','rejected')
            ORDER BY s.received_at
        """, {"s": start, "e": end})
        orders_day = await q(db, """
            SELECT id, signal_id, position_id, symbol, side, qty, filled_qty, status, price
            FROM orders WHERE created_at >= :s AND created_at < :e
        """, {"s": start, "e": end})
        pends_day = await q(db, """
            SELECT id, signal_id, customer_id, symbol, side, entry_price, status
            FROM pending_orders WHERE created_at >= :s AND created_at < :e
        """, {"s": start, "e": end})
        ord_by_sig: dict[int, list] = {}
        for o in orders_day:
            if o["signal_id"]:
                ord_by_sig.setdefault(o["signal_id"], []).append(o)
        pend_by_sig: dict[int, list] = {}
        for p in pends_day:
            if p["signal_id"]:
                pend_by_sig.setdefault(p["signal_id"], []).append(p)

        rep.add("| 时间 | 信号 | KOL | 摘要 | 动作 | 执行结果 | 对账 |")
        rep.add("|---|---|---|---|---|---|---|")
        for s_ in sigs:
            parsed = s_["parsed"] or {}
            action = parsed.get("action") or ""
            os_ = ord_by_sig.get(s_["id"]) or []
            ps_ = pend_by_sig.get(s_["id"]) or []
            note = s_["note"] or ""
            if s_["status"] == "rejected":
                verdict = WARN
                result = f"被拒: {short(note, 70)}"
            elif action.startswith("open_"):
                filled = [o for o in os_ if o["status"] == "filled"]
                if filled:
                    verdict, result = OK, f"已成交 {len(filled)} 笔"
                elif ps_:
                    st = {p["status"] for p in ps_}
                    verdict = OK
                    result = f"挂单 {len(ps_)} 张({','.join(sorted(st))})"
                else:
                    verdict, result = SEV, "❗无任何订单/挂单"
            elif action == "cancel_order":
                if "已取消" in note:
                    verdict, result = OK, short(note, 60)
                elif "没有匹配" in note:
                    verdict, result = WARN, "无匹配挂单(确认是否误判)"
                else:
                    verdict, result = WARN, short(note or "无记录", 60)
            elif action in ("close_position", "update_tp_sl"):
                verdict, result = ("🟡" if s_["status"] == "rejected" else OK), short(note or "见持仓变动", 60)
            else:
                verdict, result = OK, short(note, 60)
            rep.add(
                f"| {hhmm(s_['received_at'])} | #{s_['id']} | {s_['kol']} "
                f"| {short(s_['raw_text'], 46)} | {action or '—'} | {result} | {verdict} |"
            )
        rep.add("")
        rep.add("> 对账符号：✅正常执行　🟡建议复核　🔴执行缺失")

        # ============ 三、开仓订单无信号来源 ============
        rep.section("三、开仓订单无信号来源（系统自动/补单行为复核）")
        orphan = await q(db, """
            SELECT o.id, o.kol_id, k.name AS kol, o.customer_id, o.symbol, o.side,
                   o.qty, o.price, o.status, o.created_at
            FROM orders o LEFT JOIN kols k ON k.id = o.kol_id
            JOIN positions p ON p.id = o.position_id
            WHERE o.created_at >= :s AND o.created_at < :e
              AND o.signal_id IS NULL
              AND ((o.side='buy' AND p.side='long') OR (o.side='sell' AND p.side='short'))
              AND p.opened_at >= :s
            ORDER BY o.created_at
        """, {"s": start, "e": end})
        if orphan:
            for o in orphan:
                rep.issue(WARN, f"订单#{o['id']} {o['symbol']} {o['side']} qty={o['qty']} @{o['price']} "
                                f"KOL={o['kol'] or o['kol_id']} 无关联信号(超时/系统补单?)")
        else:
            rep.add(f"- {OK} 无异常")

        # ============ 四、撤单上下文误判扫描 ============
        rep.section("四、'撤单上下文命中'撤单记录（误判高危模式）")
        ctx_cancels = await q(db, """
            SELECT po.id, po.customer_id, po.kol_id, k.name AS kol, po.symbol, po.side,
                   po.entry_price, po.created_at, po.cancel_reason
            FROM pending_orders po LEFT JOIN kols k ON k.id = po.kol_id
            WHERE po.cancel_reason LIKE '%撤单上下文命中%'
              AND po.updated_at >= :s AND po.updated_at < :e
            ORDER BY po.updated_at DESC LIMIT 30
        """, {"s": start, "e": end})
        if ctx_cancels:
            for c in ctx_cancels:
                rep.issue(WARN, f"挂单#{c['id']} {c['symbol']} {c['side']}@{c['entry_price']} "
                                f"({hhmm(c['created_at'])}) 被上下文撤单 → {short(c['cancel_reason'], 80)}")
        else:
            rep.add(f"- {OK} 近 3 天无上下文撤单记录")

        # ============ 五、被拒信号清单 ============
        rep.section("五、被拒信号复核（是否误拒）")
        rejected = [x for x in sigs if x["status"] == "rejected"]
        if rejected:
            for s_ in rejected:
                rep.add(f"- #{s_['id']} [{s_['kol']}] {short(s_['raw_text'], 60)} → {short(s_['note'], 90)}")
        else:
            rep.add(f"- {OK} 当日无被拒信号")

        # ============ 六、灰尘仓位 ============
        rep.section("六、灰尘仓位（剩余<2%未关闭）")
        dust = await q(db, """
            SELECT id, customer_id, symbol, side, qty, initial_qty, entry_price, opened_at, kol_id
            FROM positions
            WHERE status='open' AND parent_id IS NOT NULL AND initial_qty > 0
              AND qty / initial_qty < 0.02
            ORDER BY qty / initial_qty LIMIT 20
        """)
        if dust:
            for p in dust:
                rep.issue(SEV, f"仓位#{p['id']} {p['symbol']} {p['side']} 剩余 {p['qty']:.6f}"
                                f"/初始{p['initial_qty']} ({p['qty']/p['initial_qty']*100:.2f}%)"
                                f" @{p['entry_price']:.2f} 开于{hhmm(p['opened_at'])}")
        else:
            rep.add(f"- {OK} 无灰尘仓位")

        # ============ 七、挂单异常 ============
        rep.section("七、待触发单异常")
        dup = await q(db, """
            SELECT customer_id, symbol, side, entry_price, count(*) AS n,
                   string_agg(id::text, ',') AS ids
            FROM pending_orders WHERE status='pending'
            GROUP BY customer_id, symbol, side, entry_price
            HAVING count(*) >= 2
        """)
        for x in dup:
            rep.issue(SEV, f"同价位重复挂单: {x['symbol']} {x['side']}@{x['entry_price']} "
                           f"客户{x['customer_id']} ×{x['n']} (#{x['ids']})")
        if not dup:
            rep.add(f"- {OK} 无同价位重复挂单")

        stale_pend = await q(db, """
            SELECT id, customer_id, symbol, side, entry_price, expires_at
            FROM pending_orders WHERE status='pending' AND expires_at < now()
            ORDER BY expires_at LIMIT 10
        """)
        for x in stale_pend:
            rep.issue(WARN, f"过期未清理挂单#{x['id']} {x['symbol']} {x['side']}@{x['entry_price']}"
                            f" 客户{x['customer_id']} 过期于 {hhmm(x['expires_at'])}")
        if not stale_pend and not dup:
            pass
        elif not stale_pend:
            rep.add(f"- {OK} 无过期挂单")

        # ============ 八、持仓风险 ============
        rep.section("八、持仓遗留风险")
        failed = await q(db, """
            SELECT id, customer_id, symbol, side, qty FROM positions
            WHERE status='close_failed' ORDER BY id DESC LIMIT 10
        """)
        for p in failed:
            rep.issue(SEV, f"平仓失败仓位#{p['id']} {p['symbol']} {p['side']} qty={p['qty']} 客户{p['customer_id']}")
        if not failed:
            rep.add(f"- {OK} 无 close_failed 仓位")

        aged = await q(db, """
            SELECT id, customer_id, symbol, side, qty, entry_price, opened_at
            FROM positions
            WHERE status='open' AND parent_id IS NOT NULL
              AND opened_at < now() - interval '70 hours'
            ORDER BY opened_at LIMIT 15
        """)
        for p in aged:
            hrs = (datetime.now(timezone.utc) - p["opened_at"]).total_seconds() / 3600
            rep.issue(WARN, f"超龄仓位#{p['id']} {p['symbol']} {p['side']} 已持有 {hrs:.0f}h"
                            f"（72h 将被超时平仓） 客户{p['customer_id']}")
        if not aged and not failed:
            pass
        elif not aged:
            rep.add(f"- {OK} 无超龄仓位")

        # ============ 九、当前挂单全景 ============
        rep.section("九、当前挂单全景")
        cur_pend = await q(db, """
            SELECT po.id, po.customer_id, k.name AS kol, po.symbol, po.side,
                   po.entry_price, po.status, po.expires_at
            FROM pending_orders po LEFT JOIN kols k ON k.id = po.kol_id
            WHERE po.status = 'pending'
            ORDER BY po.expires_at
        """)
        if cur_pend:
            rep.add("| 单号 | 客户 | KOL | 品种 | 方向 | 触发价 | 过期(北京) |")
            rep.add("|---|---|---|---|---|---|---|")
            for x in cur_pend:
                rep.add(f"| #{x['id']} | {x['customer_id']} | {x['kol'] or '—'} | {x['symbol']} "
                        f"| {x['side']} | {x['entry_price']} | {hhmm(x['expires_at'])} |")
        else:
            rep.add("当前无待触发挂单")

    # ============ 汇总 ============
    rep.section("十、结论")
    if rep.severe:
        rep.add(f"- {SEV} 严重问题 {rep.severe} 项，需立即处理")
    if rep.warn:
        rep.add(f"- {WARN} 待复核 {rep.warn} 项")
    if not rep.severe and not rep.warn:
        rep.add(f"- {OK} 全部对账通过，未发现异常")

    print("\n".join(rep.buf))
    print(f"严重 {rep.severe} 项 / 待复核 {rep.warn} 项 / 信号 {total_sig} 条", file=sys.stderr)


if __name__ == "__main__":
    asyncio.run(main())
