import { useMemo, useState } from "react";
import { CalendarDays, Activity, TrendingUp, TrendingDown, Wallet, ListChecks, Trophy } from "lucide-react";
import { API } from "@/api/client";
import { useFetch } from "@/lib/useFetch";
import { Badge, Card, CardTitle, Empty, MetricCard, SectionHeader } from "@/components/ui";
import { cn, fmtMoney, pnlColor } from "@/lib/utils";

type SymbolStat = {
  symbol: string;
  pnl: number;
  trade_count: number;
  open_count: number;
  close_count: number;
  fee: number;
};

type DayStat = {
  day: string;
  pnl: number;
  trade_count: number;
  open_count: number;
  close_count: number;
  fee: number;
  win_count: number;
  loss_count: number;
  symbols: SymbolStat[];
  risk_triggered?: boolean;
  risk_level?: string;
  loss_pct?: number;
  risk_snapshots?: any[];
};

function currentMonth() {
  const d = new Date();
  const y = d.getFullYear();
  const m = String(d.getMonth() + 1).padStart(2, "0");
  return `${y}-${m}`;
}

function daysInMonth(month: string) {
  const [year, mon] = month.split("-").map(Number);
  const first = new Date(year, mon - 1, 1);
  const last = new Date(year, mon, 0);
  const days: string[] = [];
  for (let d = 1; d <= last.getDate(); d++) {
    days.push(`${year}-${String(mon).padStart(2, "0")}-${String(d).padStart(2, "0")}`);
  }
  const firstWeekday = first.getDay() === 0 ? 6 : first.getDay() - 1; // 周一开始
  return { days, firstWeekday };
}

function monthShift(month: string, delta: number) {
  const [year, mon] = month.split("-").map(Number);
  const d = new Date(year, mon - 1 + delta, 1);
  return `${d.getFullYear()}-${String(d.getMonth() + 1).padStart(2, "0")}`;
}

function toneByPnl(pnl: number) {
  if (pnl > 0) return "profit" as const;
  if (pnl < 0) return "loss" as const;
  return "default" as const;
}

export default function DailyStatsPage() {
  const [month, setMonth] = useState(currentMonth());
  const { data, reload } = useFetch(() => API.dailyStats(month), [month]);
  const payload: any = data || {};
  const stats: DayStat[] = payload.days || [];
  const summary: any = payload.summary || {};

  const statByDay = useMemo(() => {
    const map = new Map<string, DayStat>();
    for (const item of stats) map.set(item.day, item);
    return map;
  }, [stats]);

  const { days, firstWeekday } = useMemo(() => daysInMonth(month), [month]);
  const selectedDefault = useMemo(() => {
    const today = new Date();
    const todayStr = `${today.getFullYear()}-${String(today.getMonth() + 1).padStart(2, "0")}-${String(today.getDate()).padStart(2, "0")}`;
    if (todayStr.startsWith(month)) return todayStr;
    return days[0];
  }, [month, days]);
  const [selected, setSelected] = useState(selectedDefault);
  const selectedDay = selected.startsWith(month) ? selected : selectedDefault;
  const selectedStat: DayStat = statByDay.get(selectedDay) || {
    day: selectedDay,
    pnl: 0,
    trade_count: 0,
    open_count: 0,
    close_count: 0,
    fee: 0,
    win_count: 0,
    loss_count: 0,
    symbols: [],
  };

  const bestDay = useMemo(() => {
    if (stats.length === 0) return null;
    return stats.reduce((best, item) => (item.pnl > best.pnl ? item : best), stats[0]);
  }, [stats]);

  const worstDay = useMemo(() => {
    if (stats.length === 0) return null;
    return stats.reduce((worst, item) => (item.pnl < worst.pnl ? item : worst), stats[0]);
  }, [stats]);

  return (
    <div className="space-y-4 md:space-y-6">
      <SectionHeader
        title="统计日历"
        subtitle="按北京时间自然日统计：每日 00:00 到次日 00:00"
        icon={CalendarDays}
        action={
          <div className="flex items-center gap-2">
            <button className="btn-ghost px-3 py-2" onClick={() => setMonth(monthShift(month, -1))}>上月</button>
            <input
              type="month"
              className="input w-[145px]"
              value={month}
              onChange={(e) => {
                setMonth(e.target.value);
                setSelected(`${e.target.value}-01`);
              }}
            />
            <button className="btn-ghost px-3 py-2" onClick={() => setMonth(monthShift(month, 1))}>下月</button>
            <button className="btn-primary px-3 py-2" onClick={reload}>刷新</button>
          </div>
        }
      />

      <div className="grid grid-cols-2 md:grid-cols-5 gap-3 md:gap-4">
        <MetricCard
          label="本月盈亏"
          value={fmtMoney(summary.total_pnl || 0)}
          icon={(summary.total_pnl || 0) >= 0 ? TrendingUp : TrendingDown}
          tone={toneByPnl(summary.total_pnl || 0)}
          trend={(summary.total_pnl || 0) >= 0 ? "up" : "down"}
        />
        <MetricCard label="交易笔数" value={summary.trade_count || 0} icon={Activity} tone="accent" />
        <MetricCard label="平仓笔数" value={summary.close_count || 0} icon={ListChecks} tone="gold" />
        <MetricCard
          label="胜率"
          value={`${summary.win_rate ?? 0}%`}
          icon={Trophy}
          tone={(summary.win_rate || 0) >= 50 ? "profit" : "default"}
        />
        <MetricCard label="手续费" value={fmtMoney(summary.fee || 0)} icon={Wallet} tone="default" />
      </div>

      <div className="grid grid-cols-1 xl:grid-cols-[1fr_360px] gap-4 md:gap-6">
        <Card>
          <CardTitle
            action={
              <div className="hidden sm:flex items-center gap-3 text-xs text-text-tertiary">
                <span className="inline-flex items-center gap-1"><span className="w-2 h-2 rounded-full bg-profit" />盈利</span>
                <span className="inline-flex items-center gap-1"><span className="w-2 h-2 rounded-full bg-loss" />亏损</span>
              </div>
            }
          >
            {month} 每日统计
          </CardTitle>

          <div className="grid grid-cols-7 gap-2 mb-2 text-center text-xs text-text-tertiary">
            {["周一", "周二", "周三", "周四", "周五", "周六", "周日"].map((w) => (
              <div key={w} className="py-1">{w}</div>
            ))}
          </div>
          <div className="grid grid-cols-7 gap-2">
            {Array.from({ length: firstWeekday }).map((_, i) => (
              <div key={`blank-${i}`} className="min-h-[88px] rounded-xl border border-transparent" />
            ))}
            {days.map((day) => {
              const stat = statByDay.get(day);
              const pnl = stat?.pnl || 0;
              const tradeCount = stat?.trade_count || 0;
              const active = selectedDay === day;
              return (
                <button
                  key={day}
                  onClick={() => setSelected(day)}
                  className={cn(
                    "min-h-[88px] rounded-xl border p-2 text-left transition-all bg-bg-card/40 hover:bg-bg-hover",
                    active ? "border-emerald shadow-[0_0_18px_-8px_rgba(0,212,160,0.6)]" : "border-border/60",
                    pnl > 0 && "bg-profit/[0.055]",
                    pnl < 0 && "bg-loss/[0.055]"
                  )}
                >
                  <div className="flex items-center justify-between gap-1">
                    <span className="text-sm font-bold text-text">{Number(day.slice(-2))}</span>
                    {tradeCount > 0 && <Badge tone={toneByPnl(pnl)} className="text-[10px]">{tradeCount}笔</Badge>}
                    {stat?.risk_triggered && <Badge tone="loss" className="text-[10px]">风控触发</Badge>}
                  </div>
                  <div className={cn("mt-3 text-sm font-bold font-mono truncate", pnlColor(pnl))}>
                    {tradeCount > 0 || stat?.risk_snapshots?.length ? fmtMoney(pnl) : "—"}
                  </div>
                  {stat?.loss_pct ? <div className="mt-1 text-[10px] text-loss font-mono">日亏损 {stat.loss_pct.toFixed(2)}%</div> : null}
                  <div className="mt-1 text-[11px] text-text-tertiary truncate">
                    平仓 {stat?.close_count || 0} · 开仓 {stat?.open_count || 0}
                  </div>
                </button>
              );
            })}
          </div>
        </Card>

        <div className="space-y-4">
          <Card>
            <CardTitle action={<Badge tone={toneByPnl(selectedStat.pnl)}>{selectedStat.trade_count} 笔</Badge>}>
              {selectedDay} 明细
            </CardTitle>
            <div className="grid grid-cols-2 gap-3">
              <div className="glass-soft p-3 rounded-xl">
                <div className="text-xs text-text-tertiary">当日盈亏</div>
                <div className={cn("text-xl font-bold font-mono mt-1", pnlColor(selectedStat.pnl))}>{fmtMoney(selectedStat.pnl)}</div>
              </div>
              <div className="glass-soft p-3 rounded-xl">
                <div className="text-xs text-text-tertiary">交易笔数</div>
                <div className="text-xl font-bold font-mono mt-1 text-text">{selectedStat.trade_count}</div>
              </div>
              <div className="glass-soft p-3 rounded-xl">
                <div className="text-xs text-text-tertiary">平仓/开仓</div>
                <div className="text-lg font-bold font-mono mt-1 text-text">{selectedStat.close_count}/{selectedStat.open_count}</div>
              </div>
              <div className="glass-soft p-3 rounded-xl">
                <div className="text-xs text-text-tertiary">手续费</div>
                <div className="text-lg font-bold font-mono mt-1 text-text">{fmtMoney(selectedStat.fee)}</div>
              </div>
            </div>
          </Card>

          <Card>
            <CardTitle>品种拆分</CardTitle>
            {selectedStat.symbols.length === 0 ? (
              <Empty text="当日暂无成交" />
            ) : (
              <div className="space-y-2">
                {selectedStat.symbols.map((item) => (
                  <div key={item.symbol} className="glass-soft p-3 rounded-xl flex items-center justify-between gap-3">
                    <div className="min-w-0">
                      <div className="font-mono text-sm font-bold text-text truncate">{item.symbol}</div>
                      <div className="text-xs text-text-tertiary mt-1">
                        {item.trade_count}笔 · 平{item.close_count} · 开{item.open_count}
                      </div>
                    </div>
                    <div className="text-right shrink-0">
                      <div className={cn("font-mono font-bold", pnlColor(item.pnl))}>{fmtMoney(item.pnl)}</div>
                      <div className="text-[11px] text-text-tertiary">费 {fmtMoney(item.fee)}</div>
                    </div>
                  </div>
                ))}
              </div>
            )}
          </Card>

          <Card>
            <CardTitle>本月极值</CardTitle>
            <div className="space-y-2 text-sm">
              <div className="flex items-center justify-between gap-3">
                <span className="text-text-tertiary">最佳日期</span>
                <span className="font-mono text-profit">{bestDay ? `${bestDay.day}  ${fmtMoney(bestDay.pnl)}` : "—"}</span>
              </div>
              <div className="flex items-center justify-between gap-3">
                <span className="text-text-tertiary">最差日期</span>
                <span className="font-mono text-loss">{worstDay ? `${worstDay.day}  ${fmtMoney(worstDay.pnl)}` : "—"}</span>
              </div>
            </div>
          </Card>
        </div>
      </div>
    </div>
  );
}
