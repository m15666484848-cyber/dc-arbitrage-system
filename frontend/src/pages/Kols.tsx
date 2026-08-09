import { useEffect, useState } from "react";
import { Crown, Check, Star, Settings2, Trophy, TrendingUp, TrendingDown, Radio } from "lucide-react";
import { API } from "@/api/client";
import { useFetch } from "@/lib/useFetch";
import { useToast } from "@/components/ui/Toast";
import { Card, CardTitle, Badge, Button, Empty, Input, Select, Field } from "@/components/ui";
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
  const { push } = useToast();

  const kols: any[] = kolsData || [];
  const ranking: any[] = rankingData || [];
  const strategies: any[] = strategiesData || [];

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
          notional_usdt: s.notional_usdt ?? null,
        });
      }
    }
    setSelected(sel);
    setSettings(map);
  }, [kolsData]);

  // 以 ranking 顺序为主,合并 KOL 跟随状态
  const kolMap = new Map(kols.map((k: any) => [k.id, k]));
  const rankedList = ranking.map((r: any) => {
    const k = kolMap.get(r.kol_id);
    return { ...r, ...k, id: r.kol_id, _rank: r };
  });
  // ranking 里没有的 KOL (新增的)追加到末尾
  for (const k of kols) {
    if (!ranking.find((r: any) => r.kol_id === k.id)) {
      rankedList.push({
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

  const saveDetail = () => {
    if (!detailKol) return;
    const t = detailKol._temp;
    setSettings((prev) => {
      const m = new Map(prev);
      m.set(detailKol.id, { kol_id: detailKol.id, strategy_id: t.strategy_id, notional_usdt: t.notional_usdt });
      return m;
    });
    setDetailKol(null);
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

  return (
    <div className="space-y-4 md:space-y-6">
      <div className="flex flex-col sm:flex-row sm:items-center justify-between gap-3">
        <div>
          <h1 className="text-xl font-bold gradient-text flex items-center gap-2">
            <Crown size={20} />
            KOL 排行
          </h1>
          <p className="text-sm text-slate-500 mt-1">按胜率排序,选择要跟单的 KOL,点击卡片可设置独立策略和金额</p>
        </div>
        <div className="flex gap-2 flex-wrap">
          <Button variant="ghost" onClick={selectAll} className="flex-1 sm:flex-initial">全选</Button>
          <Button variant="ghost" onClick={clearAll} className="flex-1 sm:flex-initial">清空</Button>
          <Button onClick={save} className="flex-1 sm:flex-initial">保存 ({selected.size})</Button>
        </div>
      </div>

      {/* 排行榜表格 */}
      <Card>
        {rankedList.length === 0 ? (
          <Empty text="暂无可用 KOL" />
        ) : (
          <div className="overflow-x-auto -mx-2">
            <table className="w-full text-sm">
              <thead>
                <tr className="text-[11px] text-slate-500 border-b border-border-soft">
                  <th className="text-center font-medium py-2 px-2 w-12">排名</th>
                  <th className="text-left font-medium py-2 px-2">KOL</th>
                  <th className="text-center font-medium py-2 px-2">跟单</th>
                  <th className="text-right font-medium py-2 px-2">胜率</th>
                  <th className="text-right font-medium py-2 px-2">交易数</th>
                  <th className="text-right font-medium py-2 px-2">信号数</th>
                  <th className="text-right font-medium py-2 px-2">总盈亏</th>
                  <th className="text-center font-medium py-2 px-2 w-16">操作</th>
                </tr>
              </thead>
              <tbody>
                {rankedList.map((kol: any, idx: number) => {
                  const r = kol._rank || {};
                  const active = selected.has(kol.id) || kol.followed;
                  const st = settings.get(kol.id);
                  const strat = st?.strategy_id ? strategies.find((s: any) => s.id === st.strategy_id) : null;
                  const rb = rankBadge(idx);
                  return (
                    <tr
                      key={kol.id}
                      className={`border-b border-border-soft/50 hover:bg-bg-hover/30 transition-colors cursor-pointer ${
                        active ? "bg-accent/5" : ""
                      }`}
                      onClick={() => handleCardClick(kol)}
                    >
                      {/* 排名 */}
                      <td className="py-3 px-2 text-center">
                        <div className={`flex items-center justify-center gap-0.5 font-bold ${rb.cls}`}>
                          {rb.icon && <rb.icon size={14} />}
                          <span className="text-sm">{rb.label}</span>
                        </div>
                      </td>

                      {/* KOL 名称 */}
                      <td className="py-3 px-2">
                        <div className="flex items-center gap-2.5">
                          <div className="w-8 h-8 rounded-lg bg-gradient-to-br from-accent/20 to-accent-glow/10 flex items-center justify-center text-accent-glow font-bold text-xs shrink-0">
                            {kol.avatar ? <img src={kol.avatar} className="w-full h-full rounded-lg object-cover" /> : kol.kol_name?.[0] || "K"}
                          </div>
                          <div className="min-w-0">
                            <div className="font-medium text-slate-200 truncate">{kol.kol_name || kol.name}</div>
                            {active && (
                              <div className="text-[10px] text-slate-500 truncate">
                                {strat ? `${strat.name}` : "默认策略"}
                                {st?.notional_usdt ? ` · ${st.notional_usdt}U` : ""}
                              </div>
                            )}
                          </div>
                        </div>
                      </td>

                      {/* 跟单状态 */}
                      <td className="py-3 px-2 text-center">
                        {active ? (
                          <div className="w-6 h-6 rounded-full bg-accent flex items-center justify-center mx-auto">
                            <Check size={14} className="text-white" />
                          </div>
                        ) : (
                          <div className="w-6 h-6 rounded-full border border-slate-600 mx-auto" />
                        )}
                      </td>

                      {/* 胜率 */}
                      <td className="py-3 px-2 text-right">
                        <span className={`font-mono font-bold ${
                          r.win_rate >= 60 ? "text-profit" : r.win_rate >= 30 ? "text-amber-400" : r.win_rate > 0 ? "text-slate-300" : "text-slate-500"
                        }`}>
                          {r.win_rate ?? 0}%
                        </span>
                      </td>

                      {/* 交易数 */}
                      <td className="py-3 px-2 text-right font-mono text-slate-400">
                        {r.trade_count ?? 0}
                      </td>

                      {/* 信号数 */}
                      <td className="py-3 px-2 text-right font-mono text-slate-400">
                        {r.signal_count ?? 0}
                      </td>

                      {/* 总盈亏 */}
                      <td className="py-3 px-2 text-right font-mono font-medium">
                        <span className={pnlColor(r.total_pnl ?? 0)}>
                          {fmtMoney(r.total_pnl ?? 0, 2)}
                        </span>
                      </td>

                      {/* 操作 */}
                      <td className="py-3 px-2 text-center">
                        {active && (
                          <button
                            onClick={(e) => { e.stopPropagation(); openDetail(kol); }}
                            className="w-7 h-7 rounded-lg bg-accent/20 hover:bg-accent/40 flex items-center justify-center mx-auto"
                            title="设置策略和金额"
                          >
                            <Settings2 size={13} className="text-accent-glow" />
                          </button>
                        )}
                      </td>
                    </tr>
                  );
                })}
              </tbody>
            </table>
          </div>
        )}
      </Card>

      {/* 统计摘要 */}
      <div className="grid grid-cols-3 gap-3">
        <div className="glass-soft p-4 text-center">
          <div className="text-xs text-slate-500 mb-1">已订阅</div>
          <div className="text-xl font-bold text-accent-glow">{selected.size}</div>
        </div>
        <div className="glass-soft p-4 text-center">
          <div className="text-xs text-slate-500 mb-1">总 KOL</div>
          <div className="text-xl font-bold text-slate-200">{rankedList.length}</div>
        </div>
        <div className="glass-soft p-4 text-center">
          <div className="text-xs text-slate-500 mb-1">平均胜率</div>
          <div className="text-xl font-bold text-slate-200">
            {rankedList.length > 0
              ? (rankedList.reduce((s: number, k: any) => s + (k._rank?.win_rate || 0), 0) / rankedList.length).toFixed(1)
              : 0}%
          </div>
        </div>
      </div>

      <Modal open={!!detailKol} onClose={() => setDetailKol(null)} title={detailKol ? `${detailKol.kol_name || detailKol.name} — 跟单设置` : ""} width="max-w-md">
        {detailKol && (
          <div className="space-y-4">
            <Field label="跟单策略">
              <Select
                value={detailKol._temp.strategy_id ?? ""}
                onChange={(e) => {
                  const v = e.target.value ? parseInt(e.target.value) : null;
                  setDetailKol({ ...detailKol, _temp: { ...detailKol._temp, strategy_id: v } });
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
            <Button className="w-full" onClick={saveDetail}>保存此 KOL 设置</Button>
            <p className="text-xs text-slate-500 text-center">提示:保存后需回到列表点击"保存"按钮才能生效</p>
          </div>
        )}
      </Modal>
    </div>
  );
}
