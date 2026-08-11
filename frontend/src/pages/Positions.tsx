import React, { useEffect, useState, useMemo, useCallback } from "react";
import { Wallet, ShieldCheck, TrendingUp, Network, ChevronDown, Briefcase, Layers, ClipboardList, Clock, XCircle, AlertTriangle, Target, ShieldAlert } from "lucide-react";
import { API } from "@/api/client";
import { useFetch } from "@/lib/useFetch";
import { useDebouncedReload } from "@/lib/useDebouncedReload";
import { useToast } from "@/components/ui/Toast";
import { wsClient } from "@/api/ws";
import { Card, CardTitle, Badge, Button, Empty, Input, SectionHeader, MetricCard } from "@/components/ui";
import { useAccountFilterStore } from "@/stores/accountFilter";
import { Modal } from "@/components/ui/Modal";
import { fmtMoney, fmtPct, pnlColor } from "@/lib/utils";

const StopBadges = React.memo(function StopBadges({ p }: { p: any }) {
  const tpLevels = Array.isArray(p.tp_levels) ? p.tp_levels : [];
  const hitTpCount = tpLevels.filter((tp: any) => tp?.status === "hit").length;
  return (
    <div className="flex flex-col items-center gap-1">
      {p.sl && <span className="text-xs font-mono text-loss">{fmtMoney(p.sl, 4)}</span>}
      {p.breakeven_moved && (
        <Badge tone="accent"><ShieldCheck size={10} /> 成本保护</Badge>
      )}
      {tpLevels.length > 0 && (
        <Badge tone="profit"><Target size={10} /> TP {hitTpCount}/{tpLevels.length}</Badge>
      )}
      {p.trailing_stop && <Badge tone="warn"><TrendingUp size={10} /> 追踪</Badge>}
    </div>
  );
});

type PositionFilter = "all" | "auto_sl" | "auto_tp" | "protected" | "abnormal";

const getTpLevels = (p: any) => Array.isArray(p?.tp_levels) ? p.tp_levels : [];
const hasAutoSl = (p: any) => Boolean(p?.sl || p?.trailing_stop || p?.breakeven_moved);
const hasAutoTp = (p: any) => getTpLevels(p).length > 0;
const hasHitTp = (p: any) => getTpLevels(p).some((tp: any) => tp?.status === "hit");
const hasAbnormalRiskState = (p: any) => {
  if (!p) return false;
  const isOpen = p.status === "open";
  const hasInvalidQty = Number(p.qty || 0) <= 0;
  const missingPrice = !p.entry_price || !p.current_price;
  const tpHitWithoutProtection = hasHitTp(p) && !p.breakeven_moved;
  return p.status !== "open" || (isOpen && (!p.sl || hasInvalidQty || missingPrice || tpHitWithoutProtection));
};

const matchPositionFilter = (p: any, filter: PositionFilter) => {
  if (filter === "all") return true;
  if (filter === "auto_sl") return hasAutoSl(p);
  if (filter === "auto_tp") return hasAutoTp(p);
  if (filter === "protected") return Boolean(p?.breakeven_moved);
  if (filter === "abnormal") return hasAbnormalRiskState(p);
  return true;
};

const ProtectionBadges = React.memo(function ProtectionBadges({ p, compact = false }: { p: any; compact?: boolean }) {
  const tpLevels = getTpLevels(p);
  const hitTpCount = tpLevels.filter((tp: any) => tp?.status === "hit").length;
  const abnormal = hasAbnormalRiskState(p);
  const badgeClass = compact ? "text-[10px]" : "text-xs";
  return (
    <div className="flex items-center gap-2 flex-wrap">
      {p.sl ? (
        <span className={`${compact ? "text-[11px]" : "text-xs"} font-mono text-loss`}>SL {fmtMoney(p.sl, 4)}</span>
      ) : (
        p.status === "open" && <Badge tone="warn" className={badgeClass}><ShieldAlert size={10} /> 无止损</Badge>
      )}
      {tpLevels.length > 0 && (
        <Badge tone="profit" className={badgeClass}><Target size={10} /> TP {hitTpCount}/{tpLevels.length}</Badge>
      )}
      {p.breakeven_moved && (
        <Badge tone="accent" className={badgeClass}><ShieldCheck size={10} /> 成本保护生效</Badge>
      )}
      {p.trailing_stop && (
        <Badge tone="warn" className={badgeClass}><TrendingUp size={10} /> 追踪止损</Badge>
      )}
      {abnormal && (
        <Badge tone="loss" className={badgeClass}><AlertTriangle size={10} /> 需检查</Badge>
      )}
    </div>
  );
});

interface ActionBtnsProps {
  p: any;
  closing: number | null;
  onStop: (p: any) => void;
  onClose: (id: number, qty?: number) => void;
}
const ActionBtns = React.memo(function ActionBtns({ p, closing, onStop, onClose }: ActionBtnsProps) {
  return (
    <div className="flex gap-1.5 justify-center">
      <Button variant="ghost" className="px-2.5 py-1 text-xs" onClick={() => onStop(p)}>
        止损
      </Button>
      <Button
        variant="danger"
        className="px-2.5 py-1 text-xs"
        disabled={closing === p.id}
        onClick={() => onClose(p.id)}
      >
        {closing === p.id ? "..." : "平仓"}
      </Button>
    </div>
  );
});

export default function PositionsPage() {
  const { accountId } = useAccountFilterStore();
  const { data, reload } = useFetch(() => API.listPositions(accountId), [accountId]);
  const { data: pendingData, reload: reloadPending } = useFetch(() => API.listPendingOrders("pending", accountId), [accountId]);
  const { push } = useToast();
  const [closing, setClosing] = useState<number | null>(null);
  const [cancelingPending, setCancelingPending] = useState<number | null>(null);
  const [viewMode, setViewMode] = useState<"positions" | "pending">("positions");
  const [positionFilter, setPositionFilter] = useState<PositionFilter>("all");
  const [stopModal, setStopModal] = useState<any>(null);
  const [stopForm, setStopForm] = useState({ sl: "", trailing_stop: false, trailing_callback: 0.01 });
  const [collapsedGroups, setCollapsedGroups] = useState<Set<number>>(new Set());
  const reloadAllPositionData = useCallback(() => {
    reload();
    reloadPending();
  }, [reload, reloadPending]);
  const reloadLiveData = useDebouncedReload(reloadAllPositionData, 700);

  useEffect(() => {
    const off = wsClient.on((event) => {
      if (event === "position" || event === "order") {
        reloadLiveData();
      }
    });
    // 轮询作为 WebSocket 的备份,统一走去抖刷新,避免和 WS 事件竞态。
    const t = setInterval(() => {
      reloadLiveData();
    }, 15000);
    return () => {
      off();
      clearInterval(t);
    };
  }, [reloadLiveData]);

  const positions: any[] = data || [];
  const pendingOrders: any[] = Array.isArray(pendingData) ? pendingData : (pendingData?.items || []);

  const totalUnrealized = useMemo(
    () => positions.reduce((sum: number, p: any) => sum + (p.unrealized_pnl || 0), 0),
    [positions]
  );

  const { groups, independent } = useMemo(() => {
    const masters = positions.filter((p) => p.parent_id === null);
    const subs = positions.filter((p) => p.parent_id !== null);

    const subByParent = new Map<number, any[]>();
    for (const sub of subs) {
      const key = sub.parent_id;
      if (!subByParent.has(key)) subByParent.set(key, []);
      subByParent.get(key)!.push(sub);
    }

    const masterIds = new Set(masters.map((m) => m.id));
    const orphanedSubs = subs.filter((s) => !masterIds.has(s.parent_id));

    const groupList: { master: any; subs: any[] }[] = [];
    const independentList: any[] = [];

    for (const master of masters) {
      const childSubs = subByParent.get(master.id) || [];
      if (childSubs.length > 0) {
        groupList.push({ master, subs: childSubs });
      } else {
        independentList.push(master);
      }
    }

    for (const orphan of orphanedSubs) {
      independentList.push(orphan);
    }

    if (groupList.length === 0 && independentList.length === 0 && subs.length > 0) return { groups: [], independent: subs };
    return { groups: groupList, independent: independentList };
  }, [positions]);

  const protectionSummary = useMemo(() => {
    const openPositions = positions.filter((p) => p.status === "open");
    return {
      all: positions.length,
      autoSl: openPositions.filter(hasAutoSl).length,
      autoTp: openPositions.filter(hasAutoTp).length,
      protected: openPositions.filter((p) => p.breakeven_moved).length,
      abnormal: positions.filter(hasAbnormalRiskState).length,
    };
  }, [positions]);

  const { visibleGroups, visibleIndependent } = useMemo(() => {
    if (positionFilter === "all") {
      return { visibleGroups: groups, visibleIndependent: independent };
    }
    return {
      visibleGroups: groups.filter(({ master, subs }) => (
        matchPositionFilter(master, positionFilter) || subs.some((sub) => matchPositionFilter(sub, positionFilter))
      )),
      visibleIndependent: independent.filter((p) => matchPositionFilter(p, positionFilter)),
    };
  }, [groups, independent, positionFilter]);

  const activeFilterLabel = useMemo(() => {
    const labels: Record<PositionFilter, string> = {
      all: "全部持仓",
      auto_sl: "自动止损",
      auto_tp: "自动止盈",
      protected: "成本保护",
      abnormal: "异常单",
    };
    return labels[positionFilter];
  }, [positionFilter]);

  const doClose = useCallback(async (id: number, qty?: number) => {
    setClosing(id);
    try {
      const r: any = await API.closePosition(id, qty);
      push("success", `已平仓,盈亏 ${fmtMoney(r.pnl)} USDT`);
      reload();
    } catch (e: any) {
      push("error", e?.response?.data?.message || "平仓失败");
    } finally {
      setClosing(null);
    }
  }, [push, reload]);


  const doCancelPending = async (pendingId: number) => {
    if (!confirm("确认取消这笔挂单吗？")) return;
    setCancelingPending(pendingId);
    try {
      await API.cancelPendingOrder(pendingId);
      push("success", "挂单已取消");
      reloadPending();
    } catch (e: any) {
      push("error", e?.response?.data?.detail || "取消挂单失败");
    } finally {
      setCancelingPending(null);
    }
  };

  const openStop = useCallback((p: any) => {
    setStopForm({ sl: p.sl?.toString() || "", trailing_stop: p.trailing_stop, trailing_callback: p.trailing_callback || 0.01 });
    setStopModal(p);
  }, []);

  const saveStop = useCallback(async () => {
    try {
      await API.updateStop({
        position_id: stopModal.id,
        sl: stopForm.sl ? parseFloat(stopForm.sl) : null,
        trailing_stop: stopForm.trailing_stop,
        trailing_callback: stopForm.trailing_callback,
      });
      push("success", "止损已更新");
      setStopModal(null);
      reload();
    } catch (e: any) {
      push("error", e?.response?.data?.message || "更新失败");
    }
  }, [stopModal, stopForm, push, reload]);

  const toggleGroup = useCallback((id: number) => {
    setCollapsedGroups((prev) => {
      const next = new Set(prev);
      if (next.has(id)) next.delete(id);
      else next.add(id);
      return next;
    });
  }, []);

  const isGroupExpanded = useCallback((id: number) => !collapsedGroups.has(id), [collapsedGroups]);

  return (
    <div className="space-y-4 md:space-y-6">
      <SectionHeader
        title="持仓管理"
        subtitle="实时持仓盈亏 · 手动平仓 · 止损/追踪止损调整"
        icon={Briefcase}
      />
      {/* multi-api-account-filter */}
      <div className="flex justify-end">
      </div>


      {/* KPI 概览 */}
      <div className="grid grid-cols-2 md:grid-cols-5 gap-3 md:gap-4">
        <MetricCard label="已持仓数" value={positions.length} icon={Wallet} tone="default" />
        <MetricCard label="挂单数" value={pendingOrders.length} icon={ClipboardList} tone="gold" />
        <MetricCard
          label="未实现盈亏"
          value={fmtMoney(totalUnrealized)}
          icon={TrendingUp}
          tone={totalUnrealized >= 0 ? "profit" : "loss"}
          trend={totalUnrealized >= 0 ? "up" : "down"}
        />
        <MetricCard label="聚合主仓" value={groups.length} icon={Network} tone="accent" />
        <MetricCard label="独立持仓" value={independent.length} icon={ShieldCheck} tone="gold" />
      </div>



      <div className="glass-soft p-1.5 flex gap-1 w-full md:w-fit">
        <button
          className={`px-4 py-2 rounded-lg text-sm font-semibold transition ${viewMode === "positions" ? "bg-emerald/[0.12] text-emerald border border-emerald-border" : "text-text-tertiary hover:text-text hover:bg-bg-hover"}`}
          onClick={() => setViewMode("positions")}
        >
          已持仓 <span className="font-mono ml-1">{positions.length}</span>
        </button>
        <button
          className={`px-4 py-2 rounded-lg text-sm font-semibold transition ${viewMode === "pending" ? "bg-amber-500/[0.12] text-amber-300 border border-amber-500/20" : "text-text-tertiary hover:text-text hover:bg-bg-hover"}`}
          onClick={() => setViewMode("pending")}
        >
          挂单 <span className="font-mono ml-1">{pendingOrders.length}</span>
        </button>
      </div>

      {viewMode === "positions" && (
        <Card className="p-3 md:p-4">
          <div className="flex flex-col md:flex-row md:items-center md:justify-between gap-3">
            <div>
              <div className="text-sm font-semibold text-slate-100">保护与异常分类</div>
              <div className="text-xs text-slate-500 mt-1">
                默认不拆散原持仓列表，只按需要筛选；第一止盈后若成本保护未生效，会进入异常单。
              </div>
            </div>
            <div className="flex items-center gap-2 text-xs text-slate-400">
              当前视图
              <Badge tone={positionFilter === "abnormal" ? "loss" : "accent"}>{activeFilterLabel}</Badge>
            </div>
          </div>
          <div className="grid grid-cols-2 md:grid-cols-5 gap-2 mt-4">
            {[
              { key: "all" as PositionFilter, label: "全部", count: protectionSummary.all, tone: "default", icon: Wallet },
              { key: "auto_sl" as PositionFilter, label: "自动止损", count: protectionSummary.autoSl, tone: "accent", icon: ShieldCheck },
              { key: "auto_tp" as PositionFilter, label: "自动止盈", count: protectionSummary.autoTp, tone: "profit", icon: Target },
              { key: "protected" as PositionFilter, label: "成本保护", count: protectionSummary.protected, tone: "gold", icon: ShieldCheck },
              { key: "abnormal" as PositionFilter, label: "异常单", count: protectionSummary.abnormal, tone: "loss", icon: AlertTriangle },
            ].map((item) => {
              const Icon = item.icon;
              const active = positionFilter === item.key;
              return (
                <button
                  key={item.key}
                  className={`rounded-xl border p-3 text-left transition ${
                    active
                      ? "border-emerald-border bg-emerald/[0.10] shadow-[0_0_18px_-8px_rgba(16,185,129,0.45)]"
                      : "border-white/10 bg-white/[0.03] hover:border-border-soft hover:bg-white/[0.05]"
                  }`}
                  onClick={() => setPositionFilter(item.key)}
                >
                  <div className="flex items-center justify-between gap-2">
                    <Badge tone={item.tone as any} className="text-[10px]">
                      <Icon size={11} /> {item.label}
                    </Badge>
                    <span className="font-mono text-base font-bold text-slate-100">{item.count}</span>
                  </div>
                </button>
              );
            })}
          </div>
        </Card>
      )}

      {viewMode === "pending" && (
        <Card>
          <CardTitle action={<Badge tone="warn"><ClipboardList size={12} /> {pendingOrders.length} 个挂单</Badge>}>
            当前挂单
          </CardTitle>
          {pendingOrders.length === 0 ? (
            <Empty text="暂无挂单" />
          ) : (
            <div className="space-y-3">
              {pendingOrders.map((o) => (
                <div key={o.id} className="glass-soft p-4 border-l-2 border-l-amber-400/60">
                  <div className="flex items-start justify-between gap-3">
                    <div className="min-w-0 flex-1">
                      <div className="flex items-center gap-2 flex-wrap">
                        <Badge tone={o.side === "long" ? "profit" : "loss"}>{o.side}</Badge>
                        <span className="font-mono text-base font-bold text-slate-100">{o.symbol}</span>
                        <Badge tone="warn" className="text-[10px] gap-1"><Clock size={10} /> 等待触发</Badge>
                        {o.trigger_mode === "condition_then_entry" && (
                          <Badge tone={o.condition_triggered_at ? "accent" : "default"} className="text-[10px]">
                            {o.condition_triggered_at ? "条件已触及" : "先等条件价"}
                          </Badge>
                        )}
                      </div>
                      <div className="grid grid-cols-2 md:grid-cols-6 gap-3 mt-3 text-xs">
                        <div>
                          <div className="text-slate-500">KOL</div>
                          <div className="text-slate-300 truncate">{o.kol_name || "未知"}</div>
                        </div>
                        <div>
                          <div className="text-slate-500">条件价</div>
                          <div className="font-mono text-amber-300">{o.condition_price ? fmtMoney(o.condition_price, 4) : "—"}</div>
                        </div>
                        <div>
                          <div className="text-slate-500">入场价</div>
                          <div className="font-mono text-slate-100">{fmtMoney(o.entry_price, 4)}</div>
                        </div>
                        <div>
                          <div className="text-slate-500">名义价值</div>
                          <div className="font-mono text-slate-300">{fmtMoney(o.notional_usdt, 2)}</div>
                        </div>
                        <div>
                          <div className="text-slate-500">杠杆</div>
                          <div className="font-mono text-slate-300">{o.leverage}x</div>
                        </div>
                        <div>
                          <div className="text-slate-500">过期</div>
                          <div className="font-mono text-slate-400">{o.expires_at ? new Date(o.expires_at).toLocaleDateString() : "—"}</div>
                        </div>
                      </div>
                      <div className="flex items-center gap-3 mt-3 text-xs flex-wrap">
                        {o.sl && <span className="font-mono text-loss">SL {fmtMoney(o.sl, 4)}</span>}
                        {o.tp_levels?.slice(0, 3).map((tp: any) => (
                          <span key={tp.level} className="font-mono text-profit">TP{tp.level} {fmtMoney(tp.price, 4)}</span>
                        ))}
                      </div>
                    </div>
                    <Button
                      variant="danger"
                      className="shrink-0 text-xs"
                      disabled={cancelingPending === o.id}
                      onClick={() => doCancelPending(o.id)}
                    >
                      <XCircle size={14} /> {cancelingPending === o.id ? "取消中" : "取消"}
                    </Button>
                  </div>
                </div>
              ))}
            </div>
          )}
        </Card>
      )}

      {viewMode === "positions" && (
      <>

            {/* 聚合主仓卡片 */}
      {visibleGroups.length > 0 && (
        <div className="space-y-4">
          {visibleGroups.map(({ master, subs }) => (
            <div key={master.id} className="glass p-5">
              <div className="flex items-start justify-between gap-4 mb-4">
                <div className="flex items-start gap-3 min-w-0 flex-1">
                  <div className="shrink-0 mt-0.5">
                    <Badge tone="accent" className="gap-1">
                      <Network size={12} />
                      聚合主仓
                    </Badge>
                  </div>
                  <div className="min-w-0 flex-1">
                    <div className="flex items-center gap-2 flex-wrap">
                      <Badge tone={master.side === "long" ? "profit" : "loss"} className="text-xs">
                        {master.side}
                      </Badge>
                      <span className="font-mono text-base font-bold text-slate-100">{master.symbol}</span>
                      {master.status !== "open" && <Badge>已平仓</Badge>}
                      <Badge tone="accent" className="text-[10px] gap-0.5">
                        <Layers size={9} /> 分批建仓
                      </Badge>
                      <span className="text-xs text-slate-500">· {subs.length} 个子仓位</span>
                    </div>
                    <div className="text-xs text-slate-500 mt-1">
                      入场 {fmtMoney(master.entry_price, 4)} · 现价 {fmtMoney(master.current_price, 4)} · 总量 {fmtMoney(master.qty, 4)}
                    </div>
                    <div className="mt-2">
                      <ProtectionBadges p={master} compact />
                    </div>
                  </div>
                </div>
                <div className="text-right shrink-0">
                  <div className={`text-lg font-bold font-mono ${pnlColor(master.unrealized_pnl)}`}>
                    {fmtMoney(master.unrealized_pnl)}
                  </div>
                  <div className={`text-sm font-mono ${pnlColor(master.pnl_pct)}`}>{fmtPct(master.pnl_pct)}</div>
                </div>
              </div>

              {/* 子仓位列表 */}
              <div className="border-t border-border pt-3">
                <button
                  className="flex items-center gap-1 text-xs text-slate-400 hover:text-slate-200 transition mb-2"
                  onClick={() => toggleGroup(master.id)}
                >
                  <ChevronDown
                    size={14}
                    className={`transition-transform ${isGroupExpanded(master.id) ? "" : "-rotate-90"}`}
                  />
                  子仓位 ({subs.length})
                </button>

                {isGroupExpanded(master.id) && (
                  <>
                    {/* 桌面端子仓位表格 */}
                    <div className="hidden md:block overflow-x-auto">
                      <table className="w-full text-sm">
                        <thead>
                          <tr className="text-xs text-slate-500 border-b border-border/50">
                            <th className="text-left py-2 px-2">KOL</th>
                            <th className="text-right px-2">入场价</th>
                            <th className="text-right px-2">数量</th>
                            <th className="text-right px-2">未实现盈亏</th>
                            <th className="text-right px-2">收益率</th>
                            <th className="text-center px-2">止损</th>
                            <th className="text-center px-2">操作</th>
                          </tr>
                        </thead>
                        <tbody>
                          {subs.map((sub) => (
                            <tr key={sub.id} className="border-b border-border/30 hover:bg-bg-hover/30">
                              <td className="py-2 px-2 text-slate-300">{sub.kol_name || "未知"}</td>
                              <td className="px-2 text-right font-mono text-slate-300">{fmtMoney(sub.entry_price, 4)}</td>
                              <td className="px-2 text-right font-mono text-slate-300">{fmtMoney(sub.qty, 4)}</td>
                              <td className={`px-2 text-right font-mono font-semibold ${pnlColor(sub.unrealized_pnl)}`}>
                                {fmtMoney(sub.unrealized_pnl)}
                              </td>
                              <td className={`px-2 text-right font-mono ${pnlColor(sub.pnl_pct)}`}>{fmtPct(sub.pnl_pct)}</td>
                              <td className="px-2 text-center">
                                {sub.sl && <span className="text-xs font-mono text-loss">{fmtMoney(sub.sl, 4)}</span>}
                                {sub.trailing_stop && (
                                  <Badge tone="warn" className="text-[10px] ml-1"><TrendingUp size={9} /> 追踪</Badge>
                                )}
                              </td>
                              <td className="px-2 text-center">
                                {sub.status === "open" ? (
                                  <ActionBtns p={sub} closing={closing} onStop={openStop} onClose={doClose} />
                                ) : (
                                  <Badge>已平仓</Badge>
                                )}
                              </td>
                            </tr>
                          ))}
                        </tbody>
                      </table>
                    </div>

                    {/* 移动端子仓位列表 */}
                    <div className="md:hidden space-y-2">
                      {subs.map((sub) => (
                        <div key={sub.id} className="glass-soft p-2.5">
                          <div className="flex items-center justify-between gap-2 mb-1.5">
                            <span className="text-xs text-slate-300 font-medium">
                              {sub.kol_name || "未知"}
                              <span className="ml-1 text-[10px] text-slate-500">#{sub.batch_no || ""}</span>
                            </span>
                            <div className={`text-sm font-bold font-mono ${pnlColor(sub.unrealized_pnl)}`}>
                              {fmtMoney(sub.unrealized_pnl)}
                            </div>
                          </div>
                          <div className="grid grid-cols-4 gap-2 text-[11px]">
                            <div>
                              <div className="text-slate-500">入场</div>
                              <div className="font-mono text-slate-300">{fmtMoney(sub.entry_price, 4)}</div>
                            </div>
                            <div>
                              <div className="text-slate-500">数量</div>
                              <div className="font-mono text-slate-300">{fmtMoney(sub.qty, 4)}</div>
                            </div>
                            <div>
                              <div className="text-slate-500">收益率</div>
                              <div className={`font-mono ${pnlColor(sub.pnl_pct)}`}>{fmtPct(sub.pnl_pct)}</div>
                            </div>
                            <div>
                              <div className="text-slate-500">止损</div>
                              <div className="font-mono text-loss">{sub.sl ? fmtMoney(sub.sl, 4) : "--"}</div>
                            </div>
                          </div>
                          {sub.status === "open" && (
                            <div className="flex gap-2 mt-2 pt-2 border-t border-border/30">
                              <Button variant="ghost" className="flex-1 text-[11px] min-h-[32px]" onClick={() => openStop(sub)}>
                                止损
                              </Button>
                              <Button
                                variant="danger"
                                className="flex-1 text-[11px] min-h-[32px]"
                                disabled={closing === sub.id}
                                onClick={() => doClose(sub.id)}
                              >
                                {closing === sub.id ? "..." : "平仓"}
                              </Button>
                            </div>
                          )}
                        </div>
                      ))}
                    </div>
                  </>
                )}
              </div>

              {/* 主仓位操作 */}
              {master.status === "open" && (
                <div className="flex gap-2 mt-4 pt-3 border-t border-border">
                  <Button variant="ghost" className="flex-1 text-sm" onClick={() => openStop(master)}>
                    调整主仓止损
                  </Button>
                  <Button
                    variant="danger"
                    className="flex-1 text-sm"
                    disabled={closing === master.id}
                    onClick={() => doClose(master.id)}
                  >
                    {closing === master.id ? "..." : "平主仓位"}
                  </Button>
                </div>
              )}
            </div>
          ))}
        </div>
      )}

      {/* 独立持仓 */}
      <Card>
        <CardTitle action={<Badge tone="accent"><Wallet size={12} /> {visibleIndependent.length} 个持仓</Badge>}>
          {groups.length > 0 ? "独立持仓" : "当前持仓"}
        </CardTitle>
        {visibleIndependent.length === 0 ? (
          groups.length === 0 ? <Empty text={positionFilter === "all" ? "暂无持仓" : `暂无${activeFilterLabel}`} /> : <Empty text={positionFilter === "all" ? "暂无独立持仓" : `暂无${activeFilterLabel}`} />
        ) : (
          <>
            {/* 桌面端表格 */}
            <div className="hidden md:block overflow-x-auto">
              <table className="w-full text-sm">
                <thead>
                  <tr className="text-xs text-slate-500 border-b border-border">
                    <th className="text-left py-2.5 px-2">KOL</th>
                    <th className="text-left px-2">品种</th>
                    <th className="text-left px-2">方向</th>
                    <th className="text-right px-2">入场价</th>
                    <th className="text-right px-2">现价</th>
                    <th className="text-right px-2">数量</th>
                    <th className="text-right px-2">未实现盈亏</th>
                    <th className="text-right px-2">收益率</th>
                    <th className="text-center px-2">止损/保护</th>
                    <th className="text-center px-2">操作</th>
                  </tr>
                </thead>
                <tbody>
                  {visibleIndependent.map((p) => (
                    <tr key={p.id} className="border-b border-border/50 hover:bg-bg-hover/40">
                      <td className="py-3 px-2 text-slate-300">{p.kol_name || "手动"}</td>
                      <td className="px-2 font-mono text-slate-100">{p.symbol}</td>
                      <td className="px-2">
                        <Badge tone={p.side === "long" ? "profit" : "loss"}>{p.side}</Badge>
                      </td>
                      <td className="px-2 text-right font-mono text-slate-300">{fmtMoney(p.entry_price, 4)}</td>
                      <td className="px-2 text-right font-mono text-slate-100">{fmtMoney(p.current_price, 4)}</td>
                      <td className="px-2 text-right font-mono text-slate-300">{fmtMoney(p.qty, 4)}</td>
                      <td className={`px-2 text-right font-mono font-semibold ${pnlColor(p.unrealized_pnl)}`}>
                        {fmtMoney(p.unrealized_pnl)}
                      </td>
                      <td className={`px-2 text-right font-mono ${pnlColor(p.pnl_pct)}`}>{fmtPct(p.pnl_pct)}</td>
                      <td className="px-2 text-center">
                        <StopBadges p={p} />
                      </td>
                      <td className="px-2 text-center">
                        {p.status === "open" ? (
                          <ActionBtns p={p} closing={closing} onStop={openStop} onClose={doClose} />
                        ) : (
                          <Badge>已平仓</Badge>
                        )}
                      </td>
                    </tr>
                  ))}
                </tbody>
              </table>
            </div>

            {/* 移动端卡片列表 */}
            <div className="md:hidden space-y-3">
              {visibleIndependent.map((p) => (
                <div key={p.id} className="glass-soft p-3.5">
                  <div className="flex items-start justify-between gap-2 mb-2.5">
                    <div className="min-w-0 flex-1">
                      <div className="flex items-center gap-1.5 flex-wrap">
                        <Badge tone={p.side === "long" ? "profit" : "loss"} className="text-[10px]">
                          {p.side}
                        </Badge>
                        <span className="font-mono text-sm font-bold text-slate-100">{p.symbol}</span>
                        {p.status !== "open" && <Badge>已平仓</Badge>}
                      </div>
                      <div className="text-xs text-slate-500 mt-1 truncate">{p.kol_name || "手动"}</div>
                    </div>
                    <div className="text-right shrink-0">
                      <div className={`text-base font-bold font-mono ${pnlColor(p.unrealized_pnl)}`}>
                        {fmtMoney(p.unrealized_pnl)}
                      </div>
                      <div className={`text-xs font-mono ${pnlColor(p.pnl_pct)}`}>{fmtPct(p.pnl_pct)}</div>
                    </div>
                  </div>

                  <div className="grid grid-cols-3 gap-2 text-xs">
                    <div>
                      <div className="text-slate-500">入场</div>
                      <div className="font-mono text-slate-300">{fmtMoney(p.entry_price, 4)}</div>
                    </div>
                    <div>
                      <div className="text-slate-500">现价</div>
                      <div className="font-mono text-slate-100">{fmtMoney(p.current_price, 4)}</div>
                    </div>
                    <div>
                      <div className="text-slate-500">数量</div>
                      <div className="font-mono text-slate-300">{fmtMoney(p.qty, 4)}</div>
                    </div>
                  </div>

                  <div className="mt-2.5">
                    <ProtectionBadges p={p} compact />
                  </div>

                  {p.status === "open" && (
                    <div className="flex gap-2 mt-3 pt-3 border-t border-border/50">
                      <Button
                        variant="ghost"
                        className="flex-1 text-xs min-h-[36px]"
                        onClick={() => openStop(p)}
                      >
                        调整止损
                      </Button>
                      <Button
                        variant="danger"
                        className="flex-1 text-xs min-h-[36px]"
                        disabled={closing === p.id}
                        onClick={() => doClose(p.id)}
                      >
                        {closing === p.id ? "..." : "立即平仓"}
                      </Button>
                    </div>
                  )}
                </div>
              ))}
            </div>
          </>
        )}
      </Card>

      </>
      )}

      <Modal open={!!stopModal} onClose={() => setStopModal(null)} title="调整止损" width="max-w-sm">
        <div className="space-y-4">
          <div>
            <label className="label">止损价</label>
            <Input
              type="number"
              value={stopForm.sl}
              onChange={(e) => setStopForm({ ...stopForm, sl: e.target.value })}
              placeholder="留空则不设置"
            />
          </div>
          <label className="flex items-center gap-2 text-sm text-slate-300 cursor-pointer min-h-[44px]">
            <input
              type="checkbox"
              checked={stopForm.trailing_stop}
              onChange={(e) => setStopForm({ ...stopForm, trailing_stop: e.target.checked })}
              className="accent-accent w-4 h-4"
            />
            启用追踪止损(盈利后按回撤比例跟进)
          </label>
          {stopForm.trailing_stop && (
            <div>
              <label className="label">回撤比例(如 0.01 = 1%)</label>
              <Input
                type="number"
                step="0.001"
                value={stopForm.trailing_callback}
                onChange={(e) => setStopForm({ ...stopForm, trailing_callback: parseFloat(e.target.value) })}
              />
            </div>
          )}
          <div className="text-xs text-slate-500 bg-bg-soft rounded-lg p-3">
            提示:达到第一止盈或 +2% 利润后,系统自动启用成本保护(止损上移至入场价+缓冲)。
          </div>
          <Button className="w-full" onClick={saveStop}>保存</Button>
        </div>
      </Modal>
    </div>
  );
}
