import { useMemo, useState } from "react";
import {
  Bar,
  BarChart,
  CartesianGrid,
  Cell,
  ComposedChart,
  Line,
  LineChart,
  Pie,
  PieChart,
  ResponsiveContainer,
  Tooltip,
  XAxis,
  YAxis,
} from "recharts";
import {
  Activity,
  BarChart3,
  LineChart as LineChartIcon,
  PieChart as PieChartIcon,
  RefreshCw,
  ShieldAlert,
  TrendingDown,
  TrendingUp,
  Wallet,
} from "lucide-react";
import { API } from "@/api/client";
import { EquityChart } from "@/components/charts/EquityChart";
import { Badge, Button, Card, CardTitle, Empty, MetricCard, SectionHeader, Select } from "@/components/ui";
import { useFetch } from "@/lib/useFetch";
import { useAccountFilterStore } from "@/stores/accountFilter";
import { cn, fmtMoney, pnlColor } from "@/lib/utils";

type EquityPoint = {
  snapshot_at: string;
  equity: number;
  balance: number;
  unrealized_pnl?: number;
};

type Trade = {
  symbol?: string;
  side?: string;
  realized_pnl?: number;
  fee?: number;
  closed_at?: string;
  created_at?: string;
};

type Position = {
  symbol?: string;
  side?: string;
  qty?: number;
  entry_price?: number;
  mark_price?: number;
  unrealized_pnl?: number;
  net_unrealized_pnl?: number;
  status?: string;
};

const COLORS = ["#38bdf8", "#22c55e", "#f0b429", "#f97316", "#a78bfa", "#fb7185", "#2dd4bf"];

function currentMonth() {
  const d = new Date();
  return `${d.getFullYear()}-${String(d.getMonth() + 1).padStart(2, "0")}`;
}

function n(value: unknown, fallback = 0) {
  const parsed = Number(value);
  return Number.isFinite(parsed) ? parsed : fallback;
}

function dateLabel(value?: string) {
  if (!value) return "—";
  return new Date(value).toLocaleDateString("zh-CN", { month: "2-digit", day: "2-digit" });
}

function chartTooltipStyle() {
  return {
    background: "#0f1420",
    border: "1px solid #2a3650",
    borderRadius: 12,
    color: "#e2e8f0",
    fontSize: 12,
    boxShadow: "0 8px 32px -8px rgba(0,0,0,0.6)",
  };
}

function moneyTooltip(value: unknown, name: unknown) {
  return [fmtMoney(n(value)), String(name)];
}

function ChartEmpty({ text = "暂无可绘制数据" }: { text?: string }) {
  return (
    <div className="h-[260px] flex items-center justify-center">
      <Empty text={text} />
    </div>
  );
}

export default function DataChartsPage() {
  const { accountId } = useAccountFilterStore();
  const [days, setDays] = useState(90);
  const [month, setMonth] = useState(currentMonth());

  const {
    data: equityData,
    loading: equityLoading,
    reload: reloadEquity,
  } = useFetch(() => API.equityCurve(days, accountId), [days, accountId]);
  const { data: tradesData, reload: reloadTrades } = useFetch(() => API.listTrades(accountId), [accountId]);
  const { data: positionsData, reload: reloadPositions } = useFetch(() => API.listPositions(accountId), [accountId]);
  const { data: dailyData, reload: reloadDaily } = useFetch(() => API.dailyStats(month, accountId), [month, accountId]);

  const equity: EquityPoint[] = Array.isArray(equityData) ? equityData : [];
  const trades: Trade[] = Array.isArray(tradesData) ? tradesData : [];
  const positions: Position[] = Array.isArray(positionsData) ? positionsData : [];
  const dailyPayload: any = dailyData || {};
  const dailyStats: any[] = dailyPayload.days || [];
  const dailySummary: any = dailyPayload.summary || {};

  const reloadAll = () => {
    reloadEquity();
    reloadTrades();
    reloadPositions();
    reloadDaily();
  };

  const equityStats = useMemo(() => {
    if (!equity.length) return { latest: 0, start: 0, change: 0, changePct: 0, high: 0, maxDrawdown: 0 };
    const start = n(equity[0].equity);
    const latest = n(equity[equity.length - 1].equity);
    const high = Math.max(...equity.map((p) => n(p.equity)));
    let peak = n(equity[0].equity);
    let maxDrawdown = 0;
    for (const point of equity) {
      const value = n(point.equity);
      peak = Math.max(peak, value);
      if (peak > 0) maxDrawdown = Math.min(maxDrawdown, ((value - peak) / peak) * 100);
    }
    const change = latest - start;
    return {
      latest,
      start,
      change,
      changePct: start > 0 ? (change / start) * 100 : 0,
      high,
      maxDrawdown,
    };
  }, [equity]);

  const drawdownData = useMemo(() => {
    let peak = 0;
    return equity.map((point) => {
      const value = n(point.equity);
      peak = Math.max(peak, value);
      return {
        time: dateLabel(point.snapshot_at),
        drawdown: peak > 0 ? ((value - peak) / peak) * 100 : 0,
      };
    });
  }, [equity]);

  const dailyChartData = useMemo(
    () =>
      dailyStats.map((item) => ({
        day: String(item.day || "").slice(5),
        pnl: n(item.pnl),
        trades: n(item.trade_count),
        fee: n(item.fee),
      })),
    [dailyStats]
  );

  const symbolPnlData = useMemo(() => {
    const map = new Map<string, { symbol: string; pnl: number; fee: number; trades: number }>();
    for (const trade of trades) {
      const symbol = trade.symbol || "UNKNOWN";
      const current = map.get(symbol) || { symbol, pnl: 0, fee: 0, trades: 0 };
      current.pnl += n(trade.realized_pnl);
      current.fee += n(trade.fee);
      current.trades += 1;
      map.set(symbol, current);
    }
    return Array.from(map.values())
      .sort((a, b) => Math.abs(b.pnl) - Math.abs(a.pnl))
      .slice(0, 10);
  }, [trades]);

  const sideData = useMemo(() => {
    const map = new Map<string, { name: string; value: number; pnl: number }>();
    for (const trade of trades) {
      const raw = String(trade.side || "unknown").toLowerCase();
      const name = raw.includes("short") || raw === "sell" ? "空单/卖出" : raw.includes("long") || raw === "buy" ? "多单/买入" : "其他";
      const current = map.get(name) || { name, value: 0, pnl: 0 };
      current.value += 1;
      current.pnl += n(trade.realized_pnl);
      map.set(name, current);
    }
    return Array.from(map.values());
  }, [trades]);

  const exposureData = useMemo(() => {
    const map = new Map<string, { symbol: string; notional: number; pnl: number }>();
    for (const position of positions.filter((p) => p.status !== "closed")) {
      const symbol = position.symbol || "UNKNOWN";
      const current = map.get(symbol) || { symbol, notional: 0, pnl: 0 };
      const price = n(position.mark_price || position.entry_price);
      current.notional += Math.abs(n(position.qty) * price);
      current.pnl += n(position.net_unrealized_pnl ?? position.unrealized_pnl);
      map.set(symbol, current);
    }
    return Array.from(map.values())
      .sort((a, b) => b.notional - a.notional)
      .slice(0, 10);
  }, [positions]);

  const winLossData = useMemo(() => {
    let win = 0;
    let loss = 0;
    let flat = 0;
    for (const trade of trades) {
      const pnl = n(trade.realized_pnl);
      if (pnl > 0) win += 1;
      else if (pnl < 0) loss += 1;
      else flat += 1;
    }
    return [
      { name: "盈利", value: win, color: "#22c55e" },
      { name: "亏损", value: loss, color: "#fb7185" },
      { name: "持平", value: flat, color: "#94a3b8" },
    ].filter((item) => item.value > 0);
  }, [trades]);

  const tradeStats = useMemo(() => {
    const totalPnl = trades.reduce((sum, trade) => sum + n(trade.realized_pnl), 0);
    const totalFee = trades.reduce((sum, trade) => sum + n(trade.fee), 0);
    const wins = trades.filter((trade) => n(trade.realized_pnl) > 0).length;
    return {
      totalPnl,
      totalFee,
      winRate: trades.length ? (wins / trades.length) * 100 : 0,
      tradeCount: trades.length,
    };
  }, [trades]);

  return (
    <div className="space-y-4 md:space-y-6">
      <SectionHeader
        title="数据图表"
        subtitle="独立图表页，不改变仪表盘；净值、回撤、盈亏、交易和持仓风险集中查看。"
        icon={BarChart3}
        action={
          <>
            <Select className="w-[120px]" value={days} onChange={(e) => setDays(Number(e.target.value))}>
              <option value={7}>近 7 天</option>
              <option value={30}>近 30 天</option>
              <option value={90}>近 90 天</option>
              <option value={180}>近 180 天</option>
            </Select>
            <input className="input w-[145px]" type="month" value={month} onChange={(e) => setMonth(e.target.value)} />
            <Button variant="ghost" className="px-3 py-2" onClick={reloadAll}>
              <RefreshCw size={15} className={equityLoading ? "animate-spin" : ""} />
              刷新
            </Button>
          </>
        }
      />

      <div className="grid grid-cols-2 lg:grid-cols-5 gap-3 md:gap-4">
        <MetricCard label="当前净值" value={fmtMoney(equityStats.latest)} icon={Wallet} tone="accent" />
        <MetricCard
          label="区间净值变化"
          value={fmtMoney(equityStats.change)}
          sub={`${equityStats.changePct >= 0 ? "+" : ""}${equityStats.changePct.toFixed(2)}%`}
          icon={equityStats.change >= 0 ? TrendingUp : TrendingDown}
          tone={equityStats.change >= 0 ? "profit" : "loss"}
          trend={equityStats.change >= 0 ? "up" : "down"}
        />
        <MetricCard label="区间高点" value={fmtMoney(equityStats.high)} icon={LineChartIcon} tone="gold" />
        <MetricCard
          label="最大回撤"
          value={`${equityStats.maxDrawdown.toFixed(2)}%`}
          icon={ShieldAlert}
          tone={equityStats.maxDrawdown < -5 ? "loss" : "default"}
          trend={equityStats.maxDrawdown < 0 ? "down" : "neutral"}
        />
        <MetricCard
          label="交易胜率"
          value={`${tradeStats.winRate.toFixed(1)}%`}
          sub={`${tradeStats.tradeCount} 笔交易`}
          icon={Activity}
          tone={tradeStats.winRate >= 50 ? "profit" : "default"}
        />
      </div>

      <div className="grid grid-cols-1 xl:grid-cols-[1.4fr_.8fr] gap-4 md:gap-6">
        <Card>
          <CardTitle action={<Badge tone="accent">{days} 天</Badge>}>账户净值 / 余额曲线</CardTitle>
          <EquityChart data={equity} height={360} />
        </Card>

        <Card>
          <CardTitle action={<Badge tone={equityStats.maxDrawdown < -5 ? "loss" : "default"}>风险观察</Badge>}>
            净值回撤
          </CardTitle>
          {drawdownData.length ? (
            <ResponsiveContainer width="100%" height={360}>
              <LineChart data={drawdownData} margin={{ top: 10, right: 10, left: 0, bottom: 0 }}>
                <CartesianGrid strokeDasharray="3 3" stroke="#1e2738" vertical={false} />
                <XAxis dataKey="time" stroke="#64748b" fontSize={11} tickLine={false} axisLine={false} minTickGap={26} />
                <YAxis
                  stroke="#64748b"
                  fontSize={11}
                  tickLine={false}
                  axisLine={false}
                  tickFormatter={(v: number) => `${v.toFixed(0)}%`}
                />
                <Tooltip contentStyle={chartTooltipStyle()} formatter={(v: unknown) => [`${n(v).toFixed(2)}%`, "回撤"]} />
                <Line type="monotone" dataKey="drawdown" stroke="#fb7185" strokeWidth={2.2} dot={false} name="回撤" />
              </LineChart>
            </ResponsiveContainer>
          ) : (
            <ChartEmpty text="暂无净值回撤数据" />
          )}
        </Card>
      </div>

      <div className="grid grid-cols-1 xl:grid-cols-2 gap-4 md:gap-6">
        <Card>
          <CardTitle action={<Badge tone="gold">{month}</Badge>}>每日盈亏与交易笔数</CardTitle>
          {dailyChartData.length ? (
            <ResponsiveContainer width="100%" height={320}>
              <ComposedChart data={dailyChartData} margin={{ top: 10, right: 10, left: 0, bottom: 0 }}>
                <CartesianGrid strokeDasharray="3 3" stroke="#1e2738" vertical={false} />
                <XAxis dataKey="day" stroke="#64748b" fontSize={11} tickLine={false} axisLine={false} />
                <YAxis yAxisId="left" stroke="#64748b" fontSize={11} tickLine={false} axisLine={false} />
                <YAxis yAxisId="right" orientation="right" stroke="#64748b" fontSize={11} tickLine={false} axisLine={false} />
                <Tooltip
                  contentStyle={chartTooltipStyle()}
                  formatter={(v: unknown, name: unknown) => (String(name) === "交易笔数" ? [n(v), String(name)] : moneyTooltip(v, name))}
                />
                <Bar yAxisId="left" dataKey="pnl" name="每日盈亏" radius={[6, 6, 0, 0]}>
                  {dailyChartData.map((item, index) => (
                    <Cell key={`${item.day}-${index}`} fill={item.pnl >= 0 ? "#22c55e" : "#fb7185"} />
                  ))}
                </Bar>
                <Line yAxisId="right" type="monotone" dataKey="trades" name="交易笔数" stroke="#38bdf8" strokeWidth={2} dot={false} />
              </ComposedChart>
            </ResponsiveContainer>
          ) : (
            <ChartEmpty text="本月暂无每日统计数据" />
          )}
        </Card>

        <Card>
          <CardTitle action={<Badge tone={tradeStats.totalPnl >= 0 ? "profit" : "loss"}>{fmtMoney(tradeStats.totalPnl)}</Badge>}>
            币种盈亏贡献
          </CardTitle>
          {symbolPnlData.length ? (
            <ResponsiveContainer width="100%" height={320}>
              <BarChart data={symbolPnlData} layout="vertical" margin={{ top: 10, right: 20, left: 18, bottom: 0 }}>
                <CartesianGrid strokeDasharray="3 3" stroke="#1e2738" horizontal={false} />
                <XAxis type="number" stroke="#64748b" fontSize={11} tickLine={false} axisLine={false} />
                <YAxis dataKey="symbol" type="category" stroke="#64748b" fontSize={11} tickLine={false} axisLine={false} width={80} />
                <Tooltip contentStyle={chartTooltipStyle()} formatter={moneyTooltip} />
                <Bar dataKey="pnl" name="已实现盈亏" radius={[0, 6, 6, 0]}>
                  {symbolPnlData.map((item, index) => (
                    <Cell key={`${item.symbol}-${index}`} fill={item.pnl >= 0 ? "#22c55e" : "#fb7185"} />
                  ))}
                </Bar>
              </BarChart>
            </ResponsiveContainer>
          ) : (
            <ChartEmpty text="暂无交易盈亏数据" />
          )}
        </Card>
      </div>

      <div className="grid grid-cols-1 xl:grid-cols-3 gap-4 md:gap-6">
        <Card>
          <CardTitle action={<PieChartIcon size={17} className="text-accent" />}>多空/买卖分布</CardTitle>
          {sideData.length ? (
            <ResponsiveContainer width="100%" height={280}>
              <PieChart>
                <Pie data={sideData} dataKey="value" nameKey="name" innerRadius={58} outerRadius={92} paddingAngle={3}>
                  {sideData.map((_, index) => (
                    <Cell key={index} fill={COLORS[index % COLORS.length]} />
                  ))}
                </Pie>
                <Tooltip contentStyle={chartTooltipStyle()} formatter={(v: unknown, name: unknown) => [`${n(v)} 笔`, String(name)]} />
              </PieChart>
            </ResponsiveContainer>
          ) : (
            <ChartEmpty text="暂无方向分布数据" />
          )}
        </Card>

        <Card>
          <CardTitle action={<Badge tone="accent">{positions.filter((p) => p.status !== "closed").length} 个</Badge>}>
            当前持仓敞口
          </CardTitle>
          {exposureData.length ? (
            <div className="space-y-3">
              {exposureData.map((item, index) => {
                const max = Math.max(...exposureData.map((row) => row.notional), 1);
                return (
                  <div key={`${item.symbol}-${index}`} className="glass-soft rounded-xl p-3">
                    <div className="flex items-center justify-between gap-2 text-sm">
                      <span className="font-bold text-text truncate">{item.symbol}</span>
                      <span className="font-mono text-text-secondary">{fmtMoney(item.notional)}</span>
                    </div>
                    <div className="mt-2 h-2 rounded-full bg-bg-hover overflow-hidden">
                      <div
                        className="h-full rounded-full bg-gradient-to-r from-accent to-emerald"
                        style={{ width: `${Math.max(4, (item.notional / max) * 100)}%` }}
                      />
                    </div>
                    <div className={cn("mt-1 text-xs font-mono", pnlColor(item.pnl))}>浮动盈亏 {fmtMoney(item.pnl)}</div>
                  </div>
                );
              })}
            </div>
          ) : (
            <ChartEmpty text="暂无当前持仓敞口" />
          )}
        </Card>

        <Card>
          <CardTitle action={<Badge tone="gold">交易质量</Badge>}>盈利 / 亏损占比</CardTitle>
          {winLossData.length ? (
            <>
              <ResponsiveContainer width="100%" height={210}>
                <PieChart>
                  <Pie data={winLossData} dataKey="value" nameKey="name" innerRadius={52} outerRadius={82} paddingAngle={3}>
                    {winLossData.map((item) => (
                      <Cell key={item.name} fill={item.color} />
                    ))}
                  </Pie>
                  <Tooltip contentStyle={chartTooltipStyle()} formatter={(v: unknown, name: unknown) => [`${n(v)} 笔`, String(name)]} />
                </PieChart>
              </ResponsiveContainer>
              <div className="grid grid-cols-2 gap-3">
                <div className="glass-soft rounded-xl p-3">
                  <div className="text-xs text-text-tertiary">手续费</div>
                  <div className="font-mono font-bold text-text mt-1">{fmtMoney(tradeStats.totalFee)}</div>
                </div>
                <div className="glass-soft rounded-xl p-3">
                  <div className="text-xs text-text-tertiary">本月盈亏</div>
                  <div className={cn("font-mono font-bold mt-1", pnlColor(dailySummary.total_pnl))}>{fmtMoney(dailySummary.total_pnl)}</div>
                </div>
              </div>
            </>
          ) : (
            <ChartEmpty text="暂无交易质量数据" />
          )}
        </Card>
      </div>
    </div>
  );
}
