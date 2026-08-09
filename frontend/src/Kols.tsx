import { useEffect, useState } from "react";
import { Crown, Check, Star, Settings2, X } from "lucide-react";
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

  const rankMap = new Map(ranking.map((r: any) => [r.kol_id, r]));

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

  return (
    <div className="space-y-4 md:space-y-6">
      <div className="flex flex-col sm:flex-row sm:items-center justify-between gap-3">
        <div>
          <h1 className="text-xl font-bold text-slate-100">KOL 排行与跟随</h1>
          <p className="text-sm text-slate-500 mt-1">多选或全选要跟单的 KOL,点击卡片设置独立策略和跟单金额</p>
        </div>
        <div className="flex gap-2 flex-wrap">
          <Button variant="ghost" onClick={selectAll} className="flex-1 sm:flex-initial">全选</Button>
          <Button variant="ghost" onClick={clearAll} className="flex-1 sm:flex-initial">清空</Button>
          <Button onClick={save} className="flex-1 sm:flex-initial">保存 ({selected.size})</Button>
        </div>
      </div>

      <div className="grid grid-cols-1 md:grid-cols-2 xl:grid-cols-3 gap-3 md:gap-4">
        {kols.length === 0 ? (
          <div className="col-span-full"><Card><Empty text="暂无可用 KOL" /></Card></div>
        ) : (
          kols.map((kol: any) => {
            const r = rankMap.get(kol.id) || {};
            const active = selected.has(kol.id) || kol.followed;
            const st = settings.get(kol.id);
            const strat = st?.strategy_id ? strategies.find((s: any) => s.id === st.strategy_id) : null;
            return (
              <div
                key={kol.id}
                className={`glass p-5 cursor-pointer transition-all border-2 ${
                  active ? "border-accent shadow-glow" : "border-transparent hover:border-border-soft"
                }`}
                onClick={() => handleCardClick(kol)}
              >
                <div className="flex items-start gap-3">
                  <div className="w-11 h-11 rounded-xl bg-gradient-to-br from-accent/30 to-accent-glow/20 flex items-center justify-center text-accent-glow font-bold">
                    {kol.avatar ? <img src={kol.avatar} className="w-full h-full rounded-xl object-cover" /> : kol.name?.[0] || "K"}
                  </div>
                  <div className="flex-1 min-w-0">
                    <div className="flex items-center gap-2">
                      <span className="font-semibold text-slate-100">{kol.name}</span>
                      {active && <Star size={14} className="text-accent-glow fill-accent-glow" />}
                    </div>
                    <div className="text-xs text-slate-500 mt-0.5 truncate">{kol.description || "—"}</div>
                  </div>
                  {(active || kol.followed) && (
                    <div className="flex items-center gap-1">
                      <button
                        onClick={(e) => { e.stopPropagation(); openDetail(kol); }}
                        className="w-6 h-6 rounded-full bg-accent/20 hover:bg-accent/40 flex items-center justify-center"
                        title="设置策略和金额"
                      >
                        <Settings2 size={12} className="text-accent-glow" />
                      </button>
                      <div className="w-6 h-6 rounded-full bg-accent flex items-center justify-center">
                        <Check size={14} className="text-white" />
                      </div>
                    </div>
                  )}
                </div>

                {(active || kol.followed) && (
                  <div className="mt-3 space-y-1.5">
                    <div className="flex items-center gap-2 text-xs">
                      <span className="text-slate-500">策略:</span>
                      <span className="text-accent-glow font-medium">{strat ? strat.name : "默认"}</span>
                      {strat && <Badge tone="accent" className="!py-0 !px-1.5 !text-[10px]">{strat.type === 'normal' ? '普通' : strat.type === 'martingale' ? '马丁' : '反马丁'}</Badge>}
                    </div>
                    <div className="flex items-center gap-2 text-xs">
                      <span className="text-slate-500">金额:</span>
                      <span className="font-mono text-slate-100">
                        {st?.notional_usdt ? `${st.notional_usdt} U` : (strat ? `${strat.params?.base_qty ?? 100} U` : "100 U")}
                      </span>
                    </div>
                  </div>
                )}

                <div className="grid grid-cols-3 gap-2 mt-3">
                  <div className="text-center glass-soft py-2">
                    <div className="text-xs text-slate-500">胜率</div>
                    <div className="text-sm font-semibold text-slate-100">{r.win_rate ?? 0}%</div>
                  </div>
                  <div className="text-center glass-soft py-2">
                    <div className="text-xs text-slate-500">信号</div>
                    <div className="text-sm font-semibold text-slate-100">{r.signal_count ?? kol.cached_signal_count ?? 0}</div>
                  </div>
                  <div className="text-center glass-soft py-2">
                    <div className="text-xs text-slate-500">盈亏</div>
                    <div className={`text-sm font-semibold font-mono ${pnlColor(r.total_pnl)}`}>
                      {fmtMoney(r.total_pnl ?? kol.cached_pnl, 0)}
                    </div>
                  </div>
                </div>
              </div>
            );
          })
        )}
      </div>

      <Modal open={!!detailKol} onClose={() => setDetailKol(null)} title={detailKol ? `${detailKol.name} — 跟单设置` : ""} width="max-w-md">
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
