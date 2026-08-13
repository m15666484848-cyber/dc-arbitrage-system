import { useEffect, useMemo, useState } from "react";
import { Link } from "react-router-dom";
import {
  ArrowUpRight,
  BarChart3,
  Check,
  Crown,
  Radio,
  Settings2,
  ShieldCheck,
  SlidersHorizontal,
  Target,
  Trophy,
  Users,
  Wallet,
} from "lucide-react";
import { API } from "@/api/client";
import { useFetch } from "@/lib/useFetch";
import { useToast } from "@/components/ui/Toast";
import { Card, Button, Empty, Input, Select, Field } from "@/components/ui";
import { Modal } from "@/components/ui/Modal";
import { fmtMoney, pnlColor } from "@/lib/utils";

interface KolSetting {
  kol_id: number;
  strategy_id: number | null;
  notional_usdt: number | null;
}

export default function KolsPage() {
  const { data: kolsData, reload } = useFetch(() => API.listKols(), []);
  const { data: rankingData } = useFetch(() => API.kolRanking(30), []);
  const { data: strategiesData } = useFetch(() => API.listStrategies(), []);
  const [selected, setSelected] = useState<Set<number>>(new Set());
  const [settings, setSettings] = useState<Map<number, KolSetting>>(new Map());
  const [detailKol, setDetailKol] = useState<any>(null);
  const [savingDetail, setSavingDetail] = useState(false);
  const { push } = useToast();

  const kols: any[] = kolsData || [];
  const ranking: any[] = rankingData || [];
  const strategies: any[] = strategiesData || [];

  const getStrategyBaseQty = (strategyId?: number | null) => {
    if (!strategyId) return null;
    const strategy = strategies.find((s: any) => s.id === strategyId);
    const qty = Number(strategy?.params?.base_qty);
    return Number.isFinite(qty) ? qty : null;
  };

  const getEffectiveNotional = (setting?: KolSetting | null) => {
    if (!setting) return { amount: 100, source: "系统默认" };
    if (setting.notional_usdt !== null && setting.notional_usdt !== undefined) {
      return { amount: setting.notional_usdt, source: "自定义" };
    }
    const strategyQty = getStrategyBaseQty(setting.strategy_id);
    if (strategyQty !== null) return { amount: strategyQty, source: "策略默认" };
    return { amount: 100, source: "系统默认" };
  };

  useEffect(() => {
    const sel = new Set<number>();
    const map = new Map<number, KolSetting>();
    for (const k of kols) {
      if (k.followed) {
        sel.add(k.id);
        const s = k.follow_settings || {};
        map.set(k.id, {
          kol_id: k.id,
          strategy_id: s.strategy_id ?? null,
          // 后端的 notional_usdt 是“最终生效金额”，可能来自策略默认值；
          // 编辑时必须使用 raw_notional_usdt，否则会把策略默认金额误存成自定义金额，导致切换策略后金额不跟随策略变化。
          notional_usdt: s.raw_notional_usdt ?? null,
        });
      }
    }
    setSelected(sel);
    setSettings(map);
  }, [kolsData]);

  // 以 ranking 顺序为主,合并 KOL 跟随状态
  const rankedList = useMemo(() => {
    const kolMap = new Map(kols.map((k: any) => [k.id, k]));
    const list = ranking.map((r: any) => {
      const k = kolMap.get(r.kol_id);
      return { ...r, ...k, id: r.kol_id, _rank: r };
    });
    // ranking 里没有的 KOL (新增的)追加到末尾
    for (const k of kols) {
      if (!ranking.find((r: any) => r.kol_id === k.id)) {
        list.push({
          kol_id: k.id,
          kol_name: k.name,
          avatar: k.avatar,
          trade_count: 0,
          signal_count: 0,
          win_rate: 0,
          total_pnl: 0,
          ...k,
          id: k.id,
          _rank: { win_rate: 0, signal_count: 0, total_pnl: 0, trade_count: 0 },
        });
      }
    }
    return list;
  }, [kols, ranking]);

  const toggle = (id: number) => {
    setSelected((s) => {
      const n = new Set(s);
      if (n.has(id)) {
        n.delete(id);
        setSettings((prev) => {
          const m = new Map(prev);
          m.delete(id);
          return m;
        });
      } else {
        n.add(id);
        setSettings((prev) => {
          const m = new Map(prev);
          if (!m.has(id)) {
            m.set(id, { kol_id: id, strategy_id: null, notional_usdt: null });
          }
          return m;
        });
      }
      return n;
    });
  };

  const handleCardClick = (kol: any) => {
    const alreadyFollowed = kols.find((k: any) => k.id === kol.id)?.followed;
    if (alreadyFollowed && selected.has(kol.id)) {
      openDetail(kol);
    } else {
      toggle(kol.id);
    }
  };

  const selectAll = () => {
    setSelected(new Set(kols.map((k: any) => k.id)));
    setSettings((prev) => {
      const m = new Map(prev);
      for (const k of kols) {
        if (!m.has(k.id)) {
          m.set(k.id, { kol_id: k.id, strategy_id: null, notional_usdt: null });
        }
      }
      return m;
    });
  };

  const clearAll = () => {
    setSelected(new Set());
    setSettings(new Map());
  };

  const openDetail = (kol: any) => {
    const alreadyFollowed = kols.find((k: any) => k.id === kol.id)?.followed;
    if (!selected.has(kol.id) && !alreadyFollowed) {
      toggle(kol.id);
    }
    const cur = settings.get(kol.id) || { kol_id: kol.id, strategy_id: null, notional_usdt: null };
    setDetailKol({ ...kol, _temp: { ...cur } });
  };

  const saveDetail = async () => {
    if (!detailKol) return;
    const t = detailKol._temp;
    const nextSelected = new Set(selected);
    nextSelected.add(detailKol.id);
    const nextSettings = new Map(settings);
    nextSettings.set(detailKol.id, { kol_id: detailKol.id, strategy_id: t.strategy_id, notional_usdt: t.notional_usdt });
    const kolSettings = Array.from(nextSelected).map((id) =>
      nextSettings.get(id) || { kol_id: id, strategy_id: null, notional_usdt: null }
    );

    try {
      setSavingDetail(true);
      await API.setFollows(kolSettings as any);
      setSelected(nextSelected);
      setSettings(nextSettings);
      push("success", "KOL 跟单设置已保存");
      setDetailKol(null);
      reload();
    } catch (e: any) {
      push("error", e?.response?.data?.message || "保存失败");
    } finally {
      setSavingDetail(false);
    }
  };

  const save = async () => {
    try {
      const kolSettings: { kol_id: number; strategy_id: number | null; notional_usdt: number | null }[] = [];
      const ids = Array.from(selected);
      for (let i = 0; i < ids.length; i++) {
        const id = ids[i];
        const existing = settings.get(id);
        kolSettings.push(existing || { kol_id: id, strategy_id: null, notional_usdt: null });
      }
      await API.setFollows(kolSettings as any);
      push("success", `已关注 ${selected.size} 个 KOL`);
      reload();
    } catch (e: any) {
      push("error", e?.response?.data?.message || "保存失败");
    }
  };

  // 排名样式
  const rankBadge = (idx: number) => {
    if (idx === 0) return { icon: Trophy, cls: "text-amber-400", label: "1" };
    if (idx === 1) return { icon: Trophy, cls: "text-slate-300", label: "2" };
    if (idx === 2) return { icon: Trophy, cls: "text-orange-600", label: "3" };
    return { icon: null, cls: "text-slate-500", label: String(idx + 1) };
  };

  const rankedStats = useMemo(() => {
    const followedCount = rankedList.filter((k: any) => selected.has(k.id) || k.followed).length;
    const averageWinRate = rankedList.length
      ? rankedList.reduce((sum: number, k: any) => sum + Number(k._rank?.win_rate || 0), 0) / rankedList.length
      : 0;
    const totalSignalCount = rankedList.reduce((sum: number, k: any) => sum + Number(k._rank?.signal_count || 0), 0);
    const totalPnl = rankedList.reduce((sum: number, k: any) => sum + Number(k._rank?.total_pnl || 0), 0);
    const bestKol = rankedList[0];
    return { followedCount, averageWinRate, totalSignalCount, totalPnl, bestKol };
  }, [rankedList, selected]);

  const topKols = rankedList.slice(0, 3);

  const rankPillClass = (idx: number) => {
    if (idx === 0) return "from-gold/25 via-gold/10 to-transparent border-gold/30 text-gold shadow-glow-gold";
    if (idx === 1) return "from-slate-300/18 via-slate-300/8 to-transparent border-slate-300/20 text-slate-200";
    if (idx === 2) return "from-orange-500/20 via-orange-500/8 to-transparent border-orange-500/25 text-orange-300";
    return "from-bg-hover/60 to-transparent border-border-soft text-text-tertiary";
  };

  const performanceTone = (value: number) =>
    value >= 60 ? "text-profit" : value >= 30 ? "text-gold" : value > 0 ? "text-text-secondary" : "text-text-tertiary";

  return (
    <div className="space-y-4 md:space-y-5 animate-fadeIn">
      <div className="glass-premium kol-rank-hero p-4 md:p-5 overflow-hidden">
        <div className="relative z-10 grid grid-cols-1 xl:grid-cols-[minmax(0,1.2fr)_minmax(420px,0.8fr)] gap-4 items-stretch">
          <div className="flex flex-col justify-between gap-5">
            <div>
              <div className="inline-flex items-center gap-2 px-2.5 py-1 rounded-full border border-gold/20 bg-gold/10 text-[11px] text-gold font-medium mb-3">
                <Crown size={13} />
                30 日表现排行
              </div>
              <h1 className="text-2xl md:text-3xl font-bold tracking-tight text-text">
                KOL 排行与跟单配置
              </h1>
              <p className="text-sm text-text-tertiary mt-2 max-w-2xl">
                按胜率、信号数量和累计盈亏快速筛选 KOL。点击行可选择跟单，已订阅的 KOL 可直接调整策略和金额。
              </p>
            </div>
            <div className="flex flex-wrap gap-2">
              <Button variant="ghost" onClick={selectAll} className="min-w-[86px]">
                全选
              </Button>
              <Button variant="ghost" onClick={clearAll} className="min-w-[86px]">
                清空
              </Button>
              <Button onClick={save} className="min-w-[128px] shadow-glow-gold">
                保存跟单 ({selected.size})
              </Button>
            </div>
          </div>

          <div className="grid grid-cols-2 gap-2.5">
            <div className="kol-rank-metric">
              <div className="flex items-center justify-between">
                <span>已订阅</span>
                <ShieldCheck size={15} className="text-profit" />
              </div>
              <strong>{rankedStats.followedCount}</strong>
              <small>当前选中的跟单对象</small>
            </div>
            <div className="kol-rank-metric">
              <div className="flex items-center justify-between">
                <span>总 KOL</span>
                <Users size={15} className="text-accent" />
              </div>
              <strong>{rankedList.length}</strong>
              <small>可配置跟随名单</small>
            </div>
            <div className="kol-rank-metric">
              <div className="flex items-center justify-between">
                <span>平均胜率</span>
                <Target size={15} className="text-gold" />
              </div>
              <strong>{rankedStats.averageWinRate.toFixed(1)}%</strong>
              <small>{rankedStats.bestKol ? `榜首 ${rankedStats.bestKol.kol_name || rankedStats.bestKol.name}` : "暂无数据"}</small>
            </div>
            <div className="kol-rank-metric">
              <div className="flex items-center justify-between">
                <span>累计盈亏</span>
                <Wallet size={15} className={rankedStats.totalPnl >= 0 ? "text-profit" : "text-loss"} />
              </div>
              <strong className={rankedStats.totalPnl >= 0 ? "text-profit" : "text-loss"}>{fmtMoney(rankedStats.totalPnl, 0)}</strong>
              <small>{rankedStats.totalSignalCount} 条信号样本</small>
            </div>
          </div>
        </div>
      </div>

      {topKols.length > 0 && (
        <div className="grid grid-cols-1 lg:grid-cols-3 gap-3 md:gap-4">
          {topKols.map((kol: any, idx: number) => {
            const r = kol._rank || {};
            const active = selected.has(kol.id) || kol.followed;
            const st = settings.get(kol.id);
            const strat = st?.strategy_id ? strategies.find((s: any) => s.id === st.strategy_id) : null;
            const effectiveNotional = getEffectiveNotional(st);
            return (
              <div
                key={`top-${kol.id}`}
                className={`kol-podium-card bg-gradient-to-br ${rankPillClass(idx)} ${active ? "is-active" : ""}`}
                onClick={() => handleCardClick(kol)}
              >
                <div className="flex items-start justify-between gap-3">
                  <div className="flex items-center gap-3 min-w-0">
                    <div className="kol-rank-medal">
                      <Trophy size={15} />
                      <span>{idx + 1}</span>
                    </div>
                    <div className="min-w-0">
                      <div className="text-base font-semibold text-text truncate">{kol.kol_name || kol.name}</div>
                      <div className="text-xs text-text-tertiary truncate mt-0.5">
                        {active ? `${strat ? strat.name : "默认策略"} · ${effectiveNotional.amount}U · ${effectiveNotional.source}` : "点击选择跟单"}
                      </div>
                    </div>
                  </div>
                  <div className={`w-8 h-8 rounded-full flex items-center justify-center shrink-0 ${active ? "bg-profit text-bg" : "bg-bg-hover text-text-tertiary"}`}>
                    {active ? <Check size={16} /> : <ArrowUpRight size={15} />}
                  </div>
                </div>
                <div className="grid grid-cols-3 gap-2 mt-4">
                  <div>
                    <span>胜率</span>
                    <strong className={performanceTone(Number(r.win_rate || 0))}>{Number(r.win_rate || 0).toFixed(1)}%</strong>
                  </div>
                  <div>
                    <span>交易</span>
                    <strong>{r.trade_count ?? 0}</strong>
                  </div>
                  <div>
                    <span>盈亏</span>
                    <strong className={pnlColor(r.total_pnl ?? 0)}>{fmtMoney(r.total_pnl ?? 0, 0)}</strong>
                  </div>
                </div>
              </div>
            );
          })}
        </div>
      )}

      <Card className="overflow-hidden !p-0">
        <div className="flex flex-col sm:flex-row sm:items-center justify-between gap-3 px-4 pt-4 pb-3 border-b border-border-soft">
          <div className="flex items-center gap-2">
            <div className="w-8 h-8 rounded-lg bg-accent/10 border border-accent/15 flex items-center justify-center">
              <BarChart3 size={16} className="text-accent" />
            </div>
            <div>
              <div className="text-sm font-semibold text-text">完整排行</div>
              <div className="text-xs text-text-tertiary">点击行选择跟单，右侧进入详情或策略设置</div>
            </div>
          </div>
          <div className="inline-flex items-center gap-1.5 text-xs text-text-tertiary">
            <SlidersHorizontal size={14} />
            胜率优先排序
          </div>
        </div>
        {rankedList.length === 0 ? (
          <Empty text="暂无可用 KOL" />
        ) : (
          <div className="overflow-x-auto">
            <table className="w-full text-sm kol-rank-table">
              <thead>
                <tr>
                  <th className="text-center w-14">排名</th>
                  <th className="text-left min-w-[360px]">KOL 信息</th>
                  <th className="text-left min-w-[170px]">跟单配置</th>
                  <th className="text-left min-w-[190px]">核心表现</th>
                  <th className="text-left min-w-[150px]">活跃度</th>
                  <th className="text-center w-28">操作</th>
                </tr>
              </thead>
              <tbody>
                {rankedList.map((kol: any, idx: number) => {
                  const r = kol._rank || {};
                  const active = selected.has(kol.id) || kol.followed;
                  const st = settings.get(kol.id);
                  const strat = st?.strategy_id ? strategies.find((s: any) => s.id === st.strategy_id) : null;
                  const effectiveNotional = getEffectiveNotional(st);
                  const rb = rankBadge(idx);
                  return (
                    <tr
                      key={kol.id}
                      className={`cursor-pointer ${
                        active ? "is-active" : ""
                      }`}
                      onClick={() => handleCardClick(kol)}
                    >
                      {/* 排名 */}
                      <td className="text-center">
                        <div className={`flex items-center justify-center gap-0.5 font-bold ${rb.cls}`}>
                          {rb.icon && <rb.icon size={14} />}
                          <span className="text-sm">{rb.label}</span>
                        </div>
                      </td>

                      {/* KOL 信息 */}
                      <td>
                        <div className="kol-rank-profile">
                          <div className={`kol-rank-avatar ${idx < 3 ? "kol-avatar" : "bg-bg-hover border border-border-soft text-text-tertiary"}`}>
                            {kol.avatar ? <img src={kol.avatar} className="w-full h-full rounded-xl object-cover" /> : kol.kol_name?.[0] || kol.name?.[0] || "K"}
                          </div>
                          <div className="min-w-0 flex-1">
                            <div className="kol-rank-name-line">
                              <span className="font-medium text-text truncate">{kol.kol_name || kol.name}</span>
                              {active && (
                                <span className="kol-rank-chip is-followed"><Check size={11} /> 已订阅</span>
                              )}
                            </div>
                            <div className="kol-rank-subline">
                              {active ? "正在跟单" : "未订阅 · 点击行加入跟单"}
                              <span className="mx-1 text-text-tertiary/50">·</span>
                              近30天排行
                            </div>
                            <div className="kol-rank-mini-stats">
                              <span className="kol-rank-mini-stat">胜率 {Number(r.win_rate || 0).toFixed(1)}%</span>
                              <span className="kol-rank-mini-stat">信号 {r.signal_count ?? 0}</span>
                              <span className={`kol-rank-mini-stat ${pnlColor(r.total_pnl ?? 0)}`}>盈亏 {fmtMoney(r.total_pnl ?? 0, 2)}</span>
                            </div>
                          </div>
                        </div>
                      </td>

                      {/* 跟单配置 */}
                      <td>
                        <div className="kol-rank-config">
                          {active ? (
                            <>
                              <div className="text-text font-medium truncate">{strat ? strat.name : "默认策略"}</div>
                              <div className="text-[10px] text-text-tertiary font-mono">
                                {effectiveNotional.amount}U · {effectiveNotional.source}
                              </div>
                            </>
                          ) : (
                            <>
                              <div className="inline-flex items-center gap-1.5 px-2 py-1 rounded-full bg-bg-hover border border-border-soft text-[11px] text-text-tertiary w-fit">
                                <Radio size={12} /> 待选择
                              </div>
                              <div className="text-[10px] text-text-tertiary mt-1">点击行快速选择</div>
                            </>
                          )}
                        </div>
                      </td>

                      {/* 核心表现 */}
                      <td>
                        <div className="kol-rank-metric-main">
                          <span className={`font-mono font-bold ${performanceTone(Number(r.win_rate || 0))}`}>
                            {Number(r.win_rate || 0).toFixed(1)}%
                          </span>
                          <span className={pnlColor(r.total_pnl ?? 0)}>{fmtMoney(r.total_pnl ?? 0, 2)}</span>
                        </div>
                        <div className="kol-rank-metric-label">胜率 / 总盈亏</div>
                      </td>

                      {/* 活跃度 */}
                      <td>
                        <div className="kol-rank-activity">
                          <span><strong>{r.trade_count ?? 0}</strong> 交易</span>
                          <span><strong>{r.signal_count ?? 0}</strong> 信号</span>
                        </div>
                      </td>

                      {/* 操作 */}
                      <td className="text-center">
                        <div className="flex items-center justify-center gap-1">
                          <Link
                            to={`/kols/${kol.id}`}
                            onClick={(e) => e.stopPropagation()}
                            className="px-2 h-7 rounded-lg bg-bg-hover hover:bg-border-soft text-[11px] text-text-secondary inline-flex items-center justify-center transition-colors"
                            title="查看 KOL 详情"
                          >
                            详情
                          </Link>
                        {active && (
                          <button
                            onClick={(e) => { e.stopPropagation(); openDetail(kol); }}
                            className="w-7 h-7 rounded-lg bg-gold/15 hover:bg-gold/25 border border-gold/15 flex items-center justify-center mx-auto transition-colors"
                            title="设置策略和金额"
                          >
                            <Settings2 size={13} className="text-gold" />
                          </button>
                        )}
                        </div>
                      </td>
                    </tr>
                  );
                })}
              </tbody>
            </table>
          </div>
        )}
      </Card>

      <Modal open={!!detailKol} onClose={() => setDetailKol(null)} title={detailKol ? `${detailKol.kol_name || detailKol.name} — 跟单设置` : ""} width="max-w-md">
        {detailKol && (
          <div className="space-y-4">
            <Field label="跟单策略">
              <Select
                value={detailKol._temp.strategy_id ?? ""}
                onChange={(e) => {
                  const v = e.target.value ? parseInt(e.target.value) : null;
                  // 切换策略时清空旧的自定义金额，避免旧 1000U 覆盖新策略默认 5000U。
                  setDetailKol({ ...detailKol, _temp: { ...detailKol._temp, strategy_id: v, notional_usdt: null } });
                }}
              >
                <option value="">默认策略 (普通, 100U)</option>
                {strategies.map((s: any) => (
                  <option key={s.id} value={s.id}>{s.name} ({s.type === 'normal' ? '普通' : s.type === 'martingale' ? '马丁' : '反马丁'} · {s.params?.base_qty ?? 100}U)</option>
                ))}
              </Select>
            </Field>
            <Field label="跟单金额 (USDT, 留空使用策略默认值)">
              <Input
                type="number"
                placeholder={detailKol._temp.strategy_id
                  ? `${strategies.find((s: any) => s.id === detailKol._temp.strategy_id)?.params?.base_qty ?? 100} (策略默认)`
                  : "100 (系统默认)"}
                value={detailKol._temp.notional_usdt ?? ""}
                onChange={(e) => {
                  const v = e.target.value ? parseFloat(e.target.value) : null;
                  setDetailKol({ ...detailKol, _temp: { ...detailKol._temp, notional_usdt: v } });
                }}
              />
            </Field>
            <Button className="w-full" onClick={saveDetail} disabled={savingDetail}>
              {savingDetail ? "保存中..." : "保存此 KOL 设置"}
            </Button>
            <p className="text-xs text-slate-500 text-center">保存后会立即生效，无需再点击列表右上角保存。</p>
          </div>
        )}
      </Modal>
    </div>
  );
}
