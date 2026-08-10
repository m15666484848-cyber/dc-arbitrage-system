import { useEffect, useMemo, useState } from "react";
import {
  TrendingDown,
  TrendingUp,
  Calendar,
  Wallet,
  Activity,
  Crown,
  Radio,
  ArrowUpRight,
  Zap,
  Layers,
  RotateCcw,
} from "lucide-react";
import { useFetch } from "@/lib/useFetch";
import { API } from "@/api/client";
import { wsClient } from "@/api/ws";
import { Card, Empty, Badge, Button } from "@/components/ui";
import { useAccountFilterStore } from "@/stores/accountFilter";
import { fmtMoney, fmtTime } from "@/lib/utils";

function PanelTitle({
  icon: Icon,
  title,
  subtitle,
  count,
  tone = "accent",
}: {
  icon: any;
  title: string;
  subtitle?: string;
  count?: number;
  tone?: "accent" | "gold";
}) {
  const iconColor = tone === "gold" ? "text-gold" : "text-accent";
  return (
    <div className="flex items-start justify-between gap-3 mb-4">
      <div className="flex items-start gap-3 min-w-0">
        <div className="stat-icon-wrap shrink-0">
          <Icon size={17} className={iconColor} />
        </div>
        <div className="min-w-0">
          <h3 className="section-title truncate">{title}</h3>
          {subtitle && <p className="text-xs text-text-tertiary mt-1">{subtitle}</p>}
        </div>
      </div>
      {typeof count === "number" && <Badge tone={tone}>{count}</Badge>}
    </div>
  );
}

function formatRoi(value?: number | null) {
  if (value === null || value === undefined || !Number.isFinite(value)) return "—";
  const sign = value > 0 ? "+" : "";
  return `${sign}${value.toFixed(2)}%`;
}

function formatHoldingDuration(openedAt?: string | null) {
  if (!openedAt) return "—";
  const openedTime = new Date(openedAt).getTime();
  if (!Number.isFinite(openedTime)) return "—";

  const diffMs = Math.max(0, Date.now() - openedTime);
  const totalMinutes = Math.floor(diffMs / 60000);
  if (totalMinutes < 1) return "刚刚";
  if (totalMinutes < 60) return `${totalMinutes}m`;

  const hours = Math.floor(totalMinutes / 60);
  const minutes = totalMinutes % 60;
  if (hours < 24) return minutes ? `${hours}h ${minutes}m` : `${hours}h`;

  const days = Math.floor(hours / 24);
  const restHours = hours % 24;
  if (days < 30) return restHours ? `${days}d ${restHours}h` : `${days}d`;

  return `${days}d`;
}

function num(value: any, fallback = 0) {
  const n = Number(value);
  return Number.isFinite(n) ? n : fallback;
}

function getPositionRoi(position: any, totalPnl: number, notional: number) {
  const backendRoi = position?.net_pnl_pct ?? position?.pnl_pct ?? position?.roi_pct;
  const parsed = Number(backendRoi);
  if (Number.isFinite(parsed)) return parsed;
  return notional > 0 ? (totalPnl / notional) * 100 : null;
}

export default function DashboardPage() {
  const { accountId } = useAccountFilterStore();
  const { data: positionsData, reload: reloadPositions } = useFetch(() => API.listPositions(accountId), [accountId]);
  const { data: tradesData } = useFetch(() => API.listTrades(accountId), [accountId]);
  const { data: dashStats, reload: reloadDash } = useFetch(() => API.dashboard(accountId), [accountId]);
  const { data: kolsData, reload: reloadKols } = useFetch(() => API.listKols(), []);
  const [resumingKolId, setResumingKolId] = useState<number | null>(null);

  // 实时刷新: WebSocket 事件 + 定时轮询
  useEffect(() => {
    const off = wsClient.on((event) => {
      if (event === "position" || event === "order") {
        reloadPositions();
        reloadDash();
        reloadKols();
      }
    });
    const t = setInterval(() => {
      reloadPositions();
      reloadDash();
      reloadKols();
    }, 15000);
    return () => {
      off();
      clearInterval(t);
    };
  }, [reloadPositions, reloadDash, reloadKols]);

  const positions: any[] = positionsData || [];
  const trades: any[] = tradesData || [];
  const s: any = dashStats || {};
  const allKols: any[] = kolsData || [];

  const openPnl = useMemo(
    () => positions.reduce((sum: number, p: any) => sum + (p.unrealized_pnl || 0), 0),
    [positions]
  );
  const realizedPnl = useMemo(
    () => trades.reduce((sum: number, t: any) => sum + (t.realized_pnl || 0), 0),
    [trades]
  );
  const totalPnl = useMemo(() => openPnl + realizedPnl, [openPnl, realizedPnl]);
  const openCount = positions.filter((p: any) => p.status === "open").length;

  // 订阅的 KOL
  const followedKols = useMemo(() => {
    const fromKols = allKols.filter((k) => k.followed);
    if (fromKols.length > 0) return fromKols;
    return s.followed_kols || [];
  }, [s.followed_kols, allKols]);

  const resumeKol = async (kolId: number) => {
    try {
      setResumingKolId(kolId);
      await API.resumeKolFollow(kolId);
      reloadKols();
      reloadDash();
    } finally {
      setResumingKolId(null);
    }
  };

  const getKolStatus = (k: any) =>
    k.follow_status || k.follow_settings?.follow_status || { status: "active", label: "正常", can_resume: false };
  const statusTone = (status: string) =>
    status === "paused" ? "loss" : status === "cooldown" ? "warn" : "profit";

  // 分批建仓: 按 parent_id 分组子仓位
  const subsByParent = useMemo(() => {
    const map = new Map<number, any[]>();
    for (const p of positions) {
      if (p.parent_id) {
        if (!map.has(p.parent_id)) map.set(p.parent_id, []);
        map.get(p.parent_id)!.push(p);
      }
    }
    return map;
  }, [positions]);

  // 当前持仓 (从 dashboard API 或 positions API)
  const openPositions = useMemo(() => {
    const fromDashboard = s.open_positions_list || [];
    if (fromDashboard.length > 0) return fromDashboard;
    return positions.filter((p: any) => p.status === "open");
  }, [s.open_positions_list, positions]);

  // 聚合后的持仓信息(包含分批子仓位的汇总)
  const aggregatedPositions = useMemo(() => {
    return openPositions.map((master: any) => {
      const subs = subsByParent.get(master.id) || [];
      if (subs.length === 0) {
        const totalQty = num(master.qty);
        const totalPnl = num(master.net_unrealized_pnl ?? master.unrealized_pnl);
        const notional = Math.abs(num(master.entry_price) * totalQty);
        return {
          ...master,
          is_batch: false,
          batch_count: 1,
          total_qty: totalQty,
          total_pnl: totalPnl,
          roi_pct: getPositionRoi(master, totalPnl, notional),
          entry_price_display: master.entry_price,
        };
      }
      // 分批建仓: master 是汇总仓位，子仓位是真实批次；避免 master + subs 重复计算
      const totalQty = subs.reduce((sum: number, sub: any) => sum + num(sub.qty), 0);
      const totalPnl = subs.reduce(
        (sum: number, sub: any) => sum + num(sub.net_unrealized_pnl ?? sub.unrealized_pnl),
        0
      );
      const totalNotional = subs.reduce(
        (sum: number, sub: any) => sum + Math.abs(num(sub.entry_price) * num(sub.qty)),
        0
      );
      const allPrices = subs.map((sub: any) => num(sub.entry_price, NaN)).filter(Number.isFinite).sort((a: number, b: number) => a - b);
      const minPrice = allPrices[0];
      const maxPrice = allPrices[allPrices.length - 1];
      return {
        ...master,
        is_batch: true,
        batch_count: subs.length,
        total_qty: totalQty,
        total_pnl: totalPnl,
        roi_pct: totalNotional > 0 ? (totalPnl / totalNotional) * 100 : getPositionRoi(master, num(master.net_unrealized_pnl ?? master.unrealized_pnl), Math.abs(num(master.entry_price) * num(master.qty))),
        entry_price_display: allPrices.length === 0 ? master.entry_price : minPrice === maxPrice ? minPrice : `${fmtMoney(minPrice)}~${fmtMoney(maxPrice)}`,
      };
    });
  }, [openPositions, subsByParent]);

  // 核心指标（第一行）
  const primaryStats = [
    { label: "持仓数量", value: openCount, tone: "default" as const, icon: Radio, sub: "当前持仓" },
    {
      label: "总盈亏",
      value: fmtMoney(totalPnl),
      tone: totalPnl >= 0 ? ("profit" as const) : ("loss" as const),
      icon: TrendingUp,
      sub: "累计表现",
    },
    {
      label: "今日盈亏",
      value: fmtMoney(s.today_pnl || 0),
      tone: (s.today_pnl || 0) >= 0 ? ("profit" as const) : ("loss" as const),
      icon: Activity,
      sub: "今日统计",
    },
  ];

  // 风险与收益指标（第二行）
  const riskStats = [
    {
      label: "最大回撤",
      value: `${s.max_drawdown ?? 0}%`,
      icon: TrendingDown,
      tone: (s.max_drawdown ?? 0) > 0 ? ("loss" as const) : ("default" as const),
      sub: "90天",
    },
    {
      label: "月化收益",
      value: `${s.monthly_return ?? 0}%`,
      icon: Calendar,
      tone: (s.monthly_return ?? 0) >= 0 ? ("profit" as const) : ("loss" as const),
      sub: "复合月化",
      trend: (s.monthly_return ?? 0) >= 0 ? ("up" as const) : ("down" as const),
    },
    {
      label: "年化收益",
      value: `${s.annual_return ?? 0}%`,
      icon: TrendingUp,
      tone: (s.annual_return ?? 0) >= 0 ? ("profit" as const) : ("loss" as const),
      sub: "复合年化",
      trend: (s.annual_return ?? 0) >= 0 ? ("up" as const) : ("down" as const),
    },
    {
      label: "夏普比率",
      value: s.sharpe_ratio ?? 0,
      icon: Activity,
      tone: (s.sharpe_ratio ?? 0) >= 1 ? ("profit" as const) : (s.sharpe_ratio ?? 0) > 0 ? ("gold" as const) : ("default" as const),
      sub: "年化夏普",
    },
  ];

  const tickerStats = [...primaryStats, ...riskStats];
  const tickerValueClass = (tone?: string) =>
    tone === "profit"
      ? "text-profit"
      : tone === "loss"
        ? "text-loss"
        : tone === "gold"
          ? "text-gold"
          : tone === "accent"
            ? "text-accent"
            : "text-text";
  const tickerTrendClass = (trend?: string) =>
    trend === "up" ? "text-up" : trend === "down" ? "text-down" : "text-text-tertiary";

  return (
    <div className="space-y-5 md:space-y-6 animate-fadeIn">
      {/* 顶部：账户总览 + 关键指标 */}
      <Card variant="premium" className="dashboard-ticker p-2">
        <div className="flex flex-col xl:flex-row gap-2 items-stretch">
          {/* 账户余额：单行紧凑行情块 */}
          <div className="hero-balance ticker-balance card-hover relative overflow-hidden px-3 py-2 xl:w-[22%] flex items-center justify-between border-l-[2px] border-l-emerald/60">
            <div className="flex items-center gap-3 min-w-0">
              <div className="stat-icon-wrap premium-glow-emerald hero-balance-icon shrink-0">
                <Wallet size={18} className="text-emerald" />
              </div>
              <div>
                <div className="flex items-center gap-1.5">
                  <Zap size={12} className="text-emerald" />
                  <span className="stat-label">账户余额</span>
                </div>
                <div className="hero-balance-value">
                  {fmtMoney(s.balance || 0)}
                </div>
              </div>
            </div>
          </div>

          {/* 关键指标：单行行情栏 */}
          <div className="flex-1 grid grid-cols-2 sm:grid-cols-4 xl:grid-cols-7 gap-2">
            {tickerStats.map((item: any) => (
              <div key={item.label} className="ticker-metric-card">
                <div className="ticker-label">{item.label}</div>
                <div className={`ticker-value ${tickerValueClass(item.tone)}`}>{item.value}</div>
                <div className="ticker-sub">
                  {item.trend && (
                    <span className={`ticker-trend ${tickerTrendClass(item.trend)}`}>
                      {item.trend === "up" ? "↑" : item.trend === "down" ? "↓" : "—"}
                    </span>
                  )}
                  <span>{item.sub || "—"}</span>
                </div>
              </div>
            ))}
          </div>
        </div>
      </Card>

      {/* 当前持仓 + 订阅 KOL */}
      <div className="grid grid-cols-1 xl:grid-cols-[minmax(0,1.7fr)_minmax(340px,0.9fr)] gap-4 md:gap-5 items-stretch">
        <div className="glass-premium p-4 md:p-5 h-full">
          <PanelTitle icon={Radio} title="当前持仓" subtitle="按仓位、方向与盈亏快速扫描" count={aggregatedPositions.length} />
          {aggregatedPositions.length ? (
            <>
              {/* 交易终端式持仓列表：首行核心信息，次行次要信息 */}
              <div className="hidden md:block space-y-2">
                {aggregatedPositions.map((p: any) => (
                  <div key={p.id} className="position-row">
                    <div className="position-row-symbol">
                      <span className="font-mono text-base font-bold text-text">{p.symbol}</span>
                        {p.exchange_account_name && <Badge tone="default" className="text-[10px]">{p.exchange_account_name}</Badge>}
                      <Badge tone={p.side === "long" ? "profit" : "loss"} className="text-[10px]">
                        {p.side === "long" ? "多" : "空"}
                      </Badge>
                      {p.is_batch && (
                        <Badge tone="accent" className="text-[10px] gap-0.5 px-1.5 py-0">
                          <Layers size={10} />
                          {p.batch_count}批
                        </Badge>
                      )}
                    </div>
                    <div className="position-row-details">
                      <span className="position-row-detail">
                        <span className="detail-label">KOL</span>
                        <span className="detail-value-text">{p.kol_name || "—"}</span>
                      </span>
                      <span className="position-row-detail">
                        <span className="detail-label">入场</span>
                        <span className="detail-value">
                          {typeof p.entry_price_display === "string"
                            ? p.entry_price_display
                            : fmtMoney(p.entry_price_display)}
                        </span>
                      </span>
                      <span className="position-row-detail">
                        <span className="detail-label">现价</span>
                        <span className="detail-value">{fmtMoney(p.current_price || p.mark_price)}</span>
                      </span>
                      <span className="position-row-detail">
                        <span className="detail-label">数量</span>
                        <span className="detail-value">{fmtMoney(p.total_qty, 4)}</span>
                      </span>
                      <span className="position-row-detail">
                        <span className="detail-label">持仓</span>
                        <span className="detail-value">{formatHoldingDuration(p.opened_at)}</span>
                      </span>
                    </div>
                    <div className="position-row-profit">
                      <div className={`position-row-pnl ${(p.total_pnl || 0) >= 0 ? "text-profit" : "text-loss"}`}>
                        {fmtMoney(p.total_pnl || 0)}
                      </div>
                      <div className={`position-row-roi ${(p.roi_pct || 0) >= 0 ? "text-profit" : "text-loss"}`}>
                        {formatRoi(p.roi_pct)}
                      </div>
                    </div>
                  </div>
                ))}
              </div>

              {/* 移动端卡片列表 */}
              <div className="md:hidden space-y-3">
                {aggregatedPositions.map((p: any) => (
                  <div key={p.id} className="glass-soft p-3.5 card-hover">
                    <div className="flex items-start justify-between gap-2 mb-2.5">
                      <div className="min-w-0 flex-1">
                        <div className="flex items-center gap-1.5 flex-wrap">
                          <Badge tone={p.side === "long" ? "profit" : "loss"} className="text-[10px]">
                            {p.side === "long" ? "多" : "空"}
                          </Badge>
                          <span className="font-mono text-sm font-bold text-text">{p.symbol}</span>
                          {p.is_batch && (
                            <Badge tone="accent" className="text-[9px] gap-0.5 px-1.5 py-0">
                              <Layers size={9} />
                              {p.batch_count}批
                            </Badge>
                          )}
                        </div>
                        <div className="text-xs text-text-tertiary mt-1 truncate">{p.kol_name || "—"}</div>
                      </div>
                      <div className="text-right shrink-0">
                        <div className={`text-base font-bold font-mono ${(p.total_pnl || 0) >= 0 ? "text-profit" : "text-loss"}`}>
                          {fmtMoney(p.total_pnl || 0)}
                        </div>
                        <div className={`text-[11px] font-mono ${(p.roi_pct || 0) >= 0 ? "text-profit" : "text-loss"}`}>
                          {formatRoi(p.roi_pct)}
                        </div>
                      </div>
                    </div>
                    <div className="grid grid-cols-3 gap-2 text-xs">
                      <div>
                        <div className="text-text-tertiary">入场</div>
                        <div className="font-mono text-text-secondary">
                          {typeof p.entry_price_display === "string"
                            ? p.entry_price_display
                            : fmtMoney(p.entry_price_display)}
                        </div>
                      </div>
                      <div>
                        <div className="text-text-tertiary">现价</div>
                        <div className="font-mono text-text">{fmtMoney(p.current_price || p.mark_price)}</div>
                      </div>
                      <div>
                        <div className="text-text-tertiary">数量</div>
                        <div className="font-mono text-text-secondary">{fmtMoney(p.total_qty, 4)}</div>
                      </div>
                    </div>
                    <div className="text-[11px] text-text-tertiary mt-2">
                      持仓 {formatHoldingDuration(p.opened_at)} · 开仓 {fmtTime(p.opened_at)}
                    </div>
                  </div>
                ))}
              </div>
            </>
          ) : (
            <Empty text="暂无持仓" />
          )}
        </div>

        <div className="glass-premium p-4 md:p-5 h-full">
          <PanelTitle icon={Crown} title="当前订阅 KOL" subtitle="跟单对象与默认下单金额" count={followedKols.length} tone="gold" />
          {followedKols.length ? (
            <div className="grid grid-cols-1 sm:grid-cols-2 xl:grid-cols-1 gap-3">
              {followedKols.map((k: any) => {
                const status = getKolStatus(k);
                const kolId = k.kol_id || k.id;
                const canResume = status.can_resume || status.status === "paused" || status.status === "cooldown";
                return (
                  <div
                    key={kolId}
                    className="flex items-center gap-3 p-3 rounded-xl glass-soft card-hover group"
                  >
                    <div className="w-9 h-9 rounded-full kol-avatar flex items-center justify-center shrink-0 transition-all">
                      <span className="text-sm font-bold text-gold">
                        {(k.kol_name || k.name || "?").charAt(0)}
                      </span>
                    </div>
                    <div className="min-w-0 flex-1">
                      <div className="flex items-center gap-2 min-w-0">
                        <div className="text-sm font-medium text-text truncate">
                          {k.kol_name || k.name}
                        </div>
                        <Badge tone={statusTone(status.status) as any} className="!py-0 !px-1.5 !text-[10px] shrink-0">
                          {status.label || "正常"}
                        </Badge>
                      </div>
                      <div className="text-[11px] text-text-tertiary truncate">
                        {k.notional_usdt || k.follow_settings?.notional_usdt
                          ? `${fmtMoney(k.notional_usdt || k.follow_settings?.notional_usdt, 0)} USDT`
                          : "100 USDT (默认)"}
                        {k.notional_source === "strategy" || k.follow_settings?.notional_source === "strategy"
                          ? <span className="text-text-muted ml-1">· 策略默认</span>
                          : k.notional_source === "default" || k.follow_settings?.notional_source === "default"
                            ? <span className="text-text-muted ml-1">· 系统默认</span>
                            : null}
                      </div>
                      {status.status === "paused" && status.paused_until && (
                        <div className="text-[10px] text-loss mt-0.5">暂停至 {fmtTime(status.paused_until)}</div>
                      )}
                      {status.status === "cooldown" && status.cooldown_until && (
                        <div className="text-[10px] text-warn mt-0.5">
                          {status.cooldown_symbol || "最近下单"} 冷却至 {fmtTime(status.cooldown_until)}
                        </div>
                      )}
                    </div>
                    {canResume ? (
                      <Button
                        variant="ghost"
                        className="px-2 py-1 text-xs gap-1 shrink-0"
                        onClick={() => resumeKol(kolId)}
                        disabled={resumingKolId === kolId}
                        title="恢复跟随并重置冷却"
                      >
                        <RotateCcw size={13} />
                        {resumingKolId === kolId ? "恢复中" : "恢复"}
                      </Button>
                    ) : (
                      <ArrowUpRight size={15} className="text-text-muted shrink-0" />
                    )}
                  </div>
                );
              })}
            </div>
          ) : (
            <Empty text="暂未订阅任何 KOL" />
          )}
        </div>
      </div>
    </div>
  );
}
