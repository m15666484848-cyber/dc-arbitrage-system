import { useState } from "react";
import { SlidersHorizontal, Plus, Pencil, Trash2 , Zap } from "lucide-react";
import { API } from "@/api/client";
import { useFetch } from "@/lib/useFetch";
import { useToast } from "@/components/ui/Toast";
import { Card, CardTitle, Badge, Button, Empty, Input, Select, Field } from "@/components/ui";
import { Modal } from "@/components/ui/Modal";

const TYPES = [
  { value: "normal", label: "普通策略", desc: "固定仓位,无加仓逻辑" },
  { value: "martingale", label: "马丁格尔", desc: "亏损后加倍,盈利重置,带熔断" },
  { value: "anti_martingale", label: "反马丁格尔", desc: "盈利后加倍,亏损重置" },
];

const EMPTY = {
  name: "", type: "normal",
  base_qty: 100, martingale_multiplier: 2, max_rounds: 3,
  default_sl_pct: -5, cost_protection_buffer: 2,
  enable_trailing: false, trailing_callback: 1, no_stop_loss: false,
  tp_levels: "3,5,8",
  use_tiered_defaults: true,
  max_sl_pct: 0,
  timeout_protection_enabled: true,
  timeout_phase1_hours: 4, timeout_phase2_hours: 24,
  timeout_phase3_hours: 72, timeout_phase4_hours: 96,
  timeout_trailing_p1: 3, timeout_trailing_p2: 2,
};

export default function StrategiesPage() {
  const { data, reload } = useFetch(() => API.listStrategies(), []);
  const { push } = useToast();
  const [modal, setModal] = useState(false);
  const [editId, setEditId] = useState<number | null>(null);
  const [f, setF] = useState<any>({ ...EMPTY });
  const [showDefault, setShowDefault] = useState(false);

  const list: any[] = data || [];

  const open = (s?: any) => {
    if (s) {
      const p = s.params || {};
      setEditId(s.id);
      setF({
        name: s.name, type: s.type,
        base_qty: p.base_qty ?? 100, martingale_multiplier: p.martingale_multiplier ?? 2,
        max_rounds: p.max_rounds ?? 3,
        default_sl_pct: (p.default_sl_pct ?? -0.05) * 100, cost_protection_buffer: (p.cost_protection_buffer ?? 0.02) * 100,
        enable_trailing: p.enable_trailing ?? false, trailing_callback: (p.trailing_callback ?? 0.01) * 100,
        no_stop_loss: p.no_stop_loss ?? false,
        tp_levels: Array.isArray(p.tp_levels?.[0]) ? (p.tp_levels || []).map((t: number[]) => Math.round(t[0] * 100)).join(",") : (p.tp_levels || [3, 5, 8]).join(","),
        use_tiered_defaults: p.use_tiered_defaults ?? (p.default_sl_pct === null || p.default_sl_pct === undefined),
        max_sl_pct: (p.max_sl_pct ?? 0) * 100,
        timeout_protection_enabled: p.timeout_protection_enabled ?? true,
        timeout_phase1_hours: p.timeout_phase1_hours ?? 4,
        timeout_phase2_hours: p.timeout_phase2_hours ?? 24,
        timeout_phase3_hours: p.timeout_phase3_hours ?? 72,
        timeout_phase4_hours: p.timeout_phase4_hours ?? 96,
        timeout_trailing_p1: (p.timeout_trailing_p1 ?? 0.03) * 100,
        timeout_trailing_p2: (p.timeout_trailing_p2 ?? 0.02) * 100,
      });
    } else {
      setEditId(null);
      setF({ ...EMPTY });
    }
    setModal(true);
  };

  const save = async () => {
    const params: Record<string, any> = {
      base_qty: Number(f.base_qty),
      default_sl_pct: f.use_tiered_defaults ? null : Number(f.default_sl_pct) / 100,
      cost_protection_buffer: Number(f.cost_protection_buffer) / 100,
      enable_trailing: f.enable_trailing,
      trailing_callback: Number(f.trailing_callback) / 100,
      no_stop_loss: f.no_stop_loss,
      use_tiered_defaults: f.use_tiered_defaults,
      tp_levels: f.use_tiered_defaults ? null : f.tp_levels.split(",").map((s: string) => Number(s.trim())),
      max_sl_pct: Number(f.max_sl_pct) > 0 ? Number(f.max_sl_pct) / 100 : undefined,
      timeout_protection_enabled: f.timeout_protection_enabled,
      timeout_phase1_hours: Number(f.timeout_phase1_hours),
      timeout_phase2_hours: Number(f.timeout_phase2_hours),
      timeout_phase3_hours: Number(f.timeout_phase3_hours),
      timeout_phase4_hours: Number(f.timeout_phase4_hours),
      timeout_trailing_p1: Number(f.timeout_trailing_p1) / 100,
      timeout_trailing_p2: Number(f.timeout_trailing_p2) / 100,
    };
    // 马丁倍数和熔断轮数仅在非普通策略时保存
    if (f.type !== "normal") {
      params.martingale_multiplier = Number(f.martingale_multiplier);
      params.max_rounds = Number(f.max_rounds);
    }
    try {
      if (editId) await API.updateStrategy(editId, { name: f.name, type: f.type, params, enabled: true });
      else await API.createStrategy({ name: f.name, type: f.type, params, enabled: true });
      push("success", "策略已保存");
      setModal(false);
      reload();
    } catch (e: any) {
      push("error", e?.response?.data?.message || "保存失败");
    }
  };

  const remove = async (id: number) => {
  if (!window.confirm("确定要删除这个策略吗？删除后不可恢复。")) return;
  try {
    await API.deleteStrategy(id);
    push("success", "策略已删除");
    reload();
  } catch (e: any) {
    push("error", e?.response?.data?.detail || "删除失败");
  }
};

  const createDefault = async () => {
    const params: Record<string, any> = {
      base_qty: 100,
      default_sl_pct: null,
      cost_protection_buffer: 0.02,
      enable_trailing: false,
      trailing_callback: 0.01,
      no_stop_loss: false,
      use_tiered_defaults: true,
      tp_levels: null,
      max_sl_pct: undefined,
      timeout_protection_enabled: true,
      timeout_phase1_hours: 4,
      timeout_phase2_hours: 24,
      timeout_phase3_hours: 72,
      timeout_phase4_hours: 96,
      timeout_trailing_p1: 0.03,
      timeout_trailing_p2: 0.02,
    };
    try {
      await API.createStrategy({ name: "普通策略", type: "normal", params, enabled: true });
      push("success", "默认策略已创建: 仓位100U · 币种分层止损 · 超时分级保护");
      reload();
    } catch (e: any) {
      push("error", e?.response?.data?.message || "创建失败");
    }
  };

  return (
    <div className="space-y-6">
      <div className="flex items-center justify-between">
        <div>
          <h1 className="text-xl font-bold gradient-text flex items-center gap-2">策略管理</h1>
          <p className="text-sm text-slate-500 mt-1">配置普通/马丁格尔/反马丁格尔策略,可在 KOL 跟随时绑定</p>
        </div>
        <Button onClick={() => open()}><Plus size={15} /> 新建策略</Button>
      </div>

      {/* 默认策略模板 */}
      <Card className="border border-dashed border-accent/30 bg-accent/5 overflow-hidden">
        {/* 折叠状态: 摘要信息 */}
        <div
          className="flex items-center justify-between cursor-pointer p-4"
          onClick={() => setShowDefault(!showDefault)}
        >
          <div className="flex items-center gap-3">
            <div className="w-10 h-10 rounded-xl bg-accent/20 flex items-center justify-center shrink-0">
              <Zap size={20} className="text-accent" />
            </div>
            <div className="flex flex-wrap items-center gap-2">
              <span className="text-sm font-medium text-slate-200">默认策略模板</span>
              <span className="px-2 py-0.5 rounded text-xs glass-soft text-slate-400">普通策略</span>
              <span className="px-2 py-0.5 rounded text-xs glass-soft text-slate-400">仓位 100U</span>
              <span className="px-2 py-0.5 rounded text-xs bg-emerald-500/10 text-emerald-400">币种分层止损</span>
              <span className="px-2 py-0.5 rounded text-xs bg-amber-500/10 text-amber-400">超时分级保护</span>
            </div>
          </div>
          <div className="flex items-center gap-3 shrink-0">
            <button
              onClick={(e) => { e.stopPropagation(); createDefault(); }}
              className="px-3 py-1.5 rounded-lg text-xs font-medium bg-accent/20 text-accent border border-accent/30 hover:bg-accent/30 transition-colors"
            >
              一键创建
            </button>
            <span className="text-xs text-slate-500">{showDefault ? "收起 ▲" : "展开 ▼"}</span>
          </div>
        </div>

        {/* 展开状态: 完整配置详情 */}
        {showDefault && (
          <div className="border-t border-slate-700 p-4 space-y-4">
            {/* 基础参数 */}
            <div>
              <div className="text-xs font-medium text-slate-400 mb-2">基础参数</div>
              <div className="grid grid-cols-2 md:grid-cols-5 gap-2">
                <div className="glass-soft p-2 rounded-lg">
                  <div className="text-slate-500 text-xs">基础仓位</div>
                  <div className="font-mono text-slate-100 text-sm">100 USDT</div>
                </div>
                <div className="glass-soft p-2 rounded-lg">
                  <div className="text-slate-500 text-xs">策略类型</div>
                  <div className="text-slate-100 text-sm">普通策略</div>
                </div>
                <div className="glass-soft p-2 rounded-lg">
                  <div className="text-slate-500 text-xs">成本保护</div>
                  <div className="font-mono text-slate-100 text-sm">2%</div>
                </div>
                <div className="glass-soft p-2 rounded-lg">
                  <div className="text-slate-500 text-xs">追踪止损</div>
                  <div className="text-slate-400 text-sm">关闭</div>
                </div>
                <div className="glass-soft p-2 rounded-lg">
                  <div className="text-slate-500 text-xs">硬止损上限</div>
                  <div className="text-slate-100 text-sm">分层默认</div>
                </div>
              </div>
            </div>

            {/* 币种分层止盈止损 */}
            <div>
              <div className="flex items-center gap-2 mb-2">
                <span className="text-emerald-400 text-xs">✓</span>
                <span className="text-xs font-medium text-slate-400">币种分层止盈止损 (KOL未提供时自动分配,与品种倍率分类同步)</span>
              </div>
              <div className="grid grid-cols-3 gap-2">
                <div className="glass-soft p-2 rounded-lg text-center">
                  <div className="text-slate-300 text-xs font-medium">主流币 (BTC/ETH)</div>
                  <div className="text-slate-500 text-xs mt-1">止损 3% · 止盈 2/4/6%</div>
                  <div className="text-slate-600 text-xs">硬上限 8%</div>
                </div>
                <div className="glass-soft p-2 rounded-lg text-center">
                  <div className="text-slate-300 text-xs font-medium">中型币 (SOL/BNB等)</div>
                  <div className="text-slate-500 text-xs mt-1">止损 5% · 止盈 3/6/10%</div>
                  <div className="text-slate-600 text-xs">硬上限 12%</div>
                </div>
                <div className="glass-soft p-2 rounded-lg text-center">
                  <div className="text-slate-300 text-xs font-medium">山寨币 (其他)</div>
                  <div className="text-slate-500 text-xs mt-1">止损 8% · 止盈 5/10/15%</div>
                  <div className="text-slate-600 text-xs">硬上限 20%</div>
                </div>
              </div>
            </div>

            {/* 超时分级保护 */}
            <div>
              <div className="flex items-center gap-2 mb-2">
                <span className="text-amber-400 text-xs">✓</span>
                <span className="text-xs font-medium text-slate-400">超时分级保护 (仅对无KOL止盈止损的持仓生效)</span>
              </div>
              <div className="grid grid-cols-2 md:grid-cols-4 gap-2">
                <div className="glass-soft p-2 rounded-lg">
                  <div className="text-amber-400 text-xs font-medium">Phase 1 · 4h</div>
                  <div className="text-slate-500 text-xs mt-1">启动追踪止损</div>
                  <div className="text-slate-600 text-xs">回调 3%</div>
                </div>
                <div className="glass-soft p-2 rounded-lg">
                  <div className="text-amber-400 text-xs font-medium">Phase 2 · 24h</div>
                  <div className="text-slate-500 text-xs mt-1">收紧止损</div>
                  <div className="text-slate-600 text-xs">回调 2%</div>
                </div>
                <div className="glass-soft p-2 rounded-lg">
                  <div className="text-amber-400 text-xs font-medium">Phase 3 · 72h</div>
                  <div className="text-slate-500 text-xs mt-1">触发飞书告警</div>
                  <div className="text-slate-600 text-xs">人工关注</div>
                </div>
                <div className="glass-soft p-2 rounded-lg">
                  <div className="text-amber-400 text-xs font-medium">Phase 4 · 96h</div>
                  <div className="text-slate-500 text-xs mt-1">自动平仓</div>
                  <div className="text-slate-600 text-xs">最终保护</div>
                </div>
              </div>
            </div>

            {/* 优先级链路 */}
            <div>
              <div className="text-xs font-medium text-slate-400 mb-1">止盈止损优先级</div>
              <div className="text-xs text-slate-500 flex items-center gap-1 flex-wrap">
                <span className="px-2 py-0.5 rounded bg-emerald-500/10 text-emerald-400">KOL信号</span>
                <span>›</span>
                <span className="px-2 py-0.5 rounded glass-soft text-slate-400">策略手动</span>
                <span>›</span>
                <span className="px-2 py-0.5 rounded glass-soft text-slate-400">币种分层</span>
                <span>›</span>
                <span className="px-2 py-0.5 rounded glass-soft text-slate-400">ATR修正</span>
                <span>›</span>
                <span className="px-2 py-0.5 rounded bg-amber-500/10 text-amber-400">超时保护</span>
              </div>
            </div>

            <div className="flex justify-end pt-1">
              <Button onClick={() => createDefault()}>
                <Zap size={15} /> 创建此策略
              </Button>
            </div>
          </div>
        )}
      </Card>

      <div className="grid md:grid-cols-2 xl:grid-cols-3 gap-4">
        {list.length === 0 ? (
          <div className="col-span-full"><Card><Empty text="暂无策略,点击右上角新建" /></Card></div>
        ) : (
          list.map((s) => {
            const t = TYPES.find((x) => x.value === s.type);
            const p = s.params || {};
            const typeIconColor = s.type === "martingale" ? "text-loss" : s.type === "anti_martingale" ? "text-accent-glow" : "text-profit";
            return (
              <Card key={s.id} className="relative overflow-hidden border border-slate-700/50 hover:border-accent/30 transition-colors group">
                {/* 顶部彩色条 */}
                <div className={`h-1 ${s.type === "martingale" ? "bg-loss" : s.type === "anti_martingale" ? "bg-accent-glow" : "bg-profit"}`} />
                <div className="p-4 space-y-2.5">
                  {/* 头部: 名称 + 操作 */}
                  <div className="flex items-start justify-between">
                    <div className="flex items-center gap-2">
                      <div className={`w-8 h-8 rounded-lg flex items-center justify-center shrink-0 ${s.type === "martingale" ? "bg-loss/15" : s.type === "anti_martingale" ? "bg-accent/15" : "bg-profit/15"}`}>
                        <SlidersHorizontal size={15} className={typeIconColor} />
                      </div>
                      <div>
                        <div className="font-semibold text-slate-100">{s.name}</div>
                        <div className="text-xs text-slate-500">{t?.label}</div>
                      </div>
                    </div>
                    <div className="flex gap-1 opacity-0 group-hover:opacity-100 transition-opacity">
                      <button onClick={() => open(s)} className="text-slate-400 hover:text-accent-glow p-1"><Pencil size={14} /></button>
                      <button onClick={() => remove(s.id)} className="text-slate-400 hover:text-loss p-1"><Trash2 size={14} /></button>
                    </div>
                  </div>

                  {/* 基础仓位 */}
                  <div className="glass-soft p-2 rounded-lg">
                    <div className="text-slate-500 text-xs">基础仓位</div>
                    <div className="font-mono text-slate-100 text-sm">{p.base_qty} U</div>
                  </div>

                  {/* 止盈止损配置 */}
                  <div className="glass-soft p-2 rounded-lg">
                    <div className="text-slate-500 text-xs mb-1">止盈止损</div>
                    {p.no_stop_loss ? (
                      <div className="text-sm text-red-400">无止损 · 仅硬上限兜底</div>
                    ) : p.use_tiered_defaults !== false ? (
                      <div className="text-xs space-y-0.5">
                        <div className="flex items-center gap-1.5">
                          <span className="px-1.5 py-0.5 rounded bg-emerald-500/10 text-emerald-400">分层</span>
                          <span className="text-slate-300">BTC/ETH: SL3% TP2/4/6%</span>
                        </div>
                        <div className="text-slate-500 pl-9">中型: SL5% TP3/6/10% · 山寨: SL8% TP5/10/15%</div>
                      </div>
                    ) : (
                      <div className="flex items-center gap-1.5 text-xs">
                        <span className="px-1.5 py-0.5 rounded glass-soft text-slate-400">手动</span>
                        <span className="text-slate-300">SL {p.default_sl_pct ? `${(p.default_sl_pct * 100).toFixed(0)}%` : "—"}</span>
                        <span className="text-slate-600">·</span>
                        <span className="text-slate-300">TP {Array.isArray(p.tp_levels) ? p.tp_levels.join("/") : "—"}%</span>
                      </div>
                    )}
                    {Number(p.max_sl_pct) > 0 && (
                      <div className="text-xs text-amber-400 mt-1">硬上限 {(p.max_sl_pct * 100).toFixed(0)}% (覆盖分层)</div>
                    )}
                  </div>

                  {/* 追踪止损 (仅启用时显示) */}
                  {p.enable_trailing && (
                    <div className="glass-soft p-2 rounded-lg">
                      <div className="text-slate-500 text-xs mb-1">追踪止损</div>
                      <div className="grid grid-cols-2 gap-2">
                        <div>
                          <div className="text-xs text-slate-500">回撤</div>
                          <div className="text-sm text-blue-400">{((p.trailing_callback ?? 0.01) * 100).toFixed(1)}%</div>
                        </div>
                        <div>
                          <div className="text-xs text-slate-500">成本缓冲</div>
                          <div className="text-sm text-blue-400">{((p.cost_protection_buffer ?? 0.02) * 100).toFixed(1)}%</div>
                        </div>
                      </div>
                    </div>
                  )}

                  {/* 超时保护 (仅启用时显示) */}
                  {p.timeout_protection_enabled !== false && (
                    <div className="glass-soft p-2 rounded-lg">
                      <div className="text-slate-500 text-xs mb-1">超时分级保护</div>
                      <div className="text-xs flex items-center gap-1 flex-wrap">
                        <span className="text-amber-400">{p.timeout_phase1_hours ?? 4}h追踪</span>
                        <span className="text-slate-600">›</span>
                        <span className="text-amber-400">{p.timeout_phase2_hours ?? 24}h收紧</span>
                        <span className="text-slate-600">›</span>
                        <span className="text-rose-400">{p.timeout_phase3_hours ?? 72}h告警</span>
                        <span className="text-slate-600">›</span>
                        <span className="text-red-400">{p.timeout_phase4_hours ?? 96}h平仓</span>
                      </div>
                    </div>
                  )}

                  {/* 马丁参数 + 状态 */}
                  {s.type !== "normal" && (
                    <>
                      <div className="grid grid-cols-2 gap-2">
                        <div className="glass-soft p-2 rounded-lg">
                          <div className="text-slate-500 text-xs">马丁倍数</div>
                          <div className="font-mono text-slate-100 text-sm">{p.martingale_multiplier}x</div>
                        </div>
                        <div className="glass-soft p-2 rounded-lg">
                          <div className="text-slate-500 text-xs">熔断轮数</div>
                          <div className="font-mono text-slate-100 text-sm">{p.max_rounds}</div>
                        </div>
                      </div>
                      <div className="flex items-center gap-2 text-xs">
                        <span className="text-slate-500">当前轮次</span>
                        <span className="font-mono text-slate-300">{s.martingale_round}</span>
                        <span className="text-slate-600">·</span>
                        <span className={`px-1.5 py-0.5 rounded ${s.last_result === "win" ? "bg-profit/10 text-profit" : s.last_result === "loss" ? "bg-loss/10 text-loss" : "glass-soft text-slate-400"}`}>
                          {s.last_result === "win" ? "盈利" : s.last_result === "loss" ? "亏损" : "—"}
                        </span>
                      </div>
                    </>
                  )}
                </div>
              </Card>
            );
          })
        )}
      </div>

      <Modal open={modal} onClose={() => setModal(false)} title={editId ? "编辑策略" : "新建策略"} width="max-w-2xl">
        <div className="space-y-5">
          {/* ── Step 1: 基础信息 ── */}
          <div className="space-y-3">
            <div className="flex items-center gap-2">
              <div className="w-7 h-7 rounded-lg bg-accent/20 flex items-center justify-center text-xs font-bold text-accent shrink-0">1</div>
              <div>
                <div className="text-sm font-semibold text-slate-200">基础信息</div>
                <div className="text-xs text-slate-500">策略名称、类型与仓位参数</div>
              </div>
            </div>
            <div className="grid grid-cols-2 gap-3 pl-9">
              <Field label="策略名称"><Input value={f.name} onChange={(e) => setF({ ...f, name: e.target.value })} /></Field>
              <Field label="策略类型">
                <Select value={f.type} onChange={(e) => setF({ ...f, type: e.target.value })}>
                  {TYPES.map((t) => <option key={t.value} value={t.value}>{t.label}</option>)}
                </Select>
              </Field>
            </div>
            <div className="grid grid-cols-3 gap-3 pl-9">
              <Field label="基础仓位(USDT)"><Input type="number" value={f.base_qty} onChange={(e) => setF({ ...f, base_qty: e.target.value })} /></Field>
              {f.type !== "normal" && (
                <>
                  <Field label="马丁倍数"><Input type="number" step="0.1" value={f.martingale_multiplier} onChange={(e) => setF({ ...f, martingale_multiplier: e.target.value })} /></Field>
                  <Field label="熔断轮数"><Input type="number" value={f.max_rounds} onChange={(e) => setF({ ...f, max_rounds: e.target.value })} /></Field>
                </>
              )}
            </div>
            <div className="pl-9 text-xs text-slate-500 flex items-center gap-1.5">
              <span className="text-accent/60">▸</span>
              {TYPES.find((t) => t.value === f.type)?.desc}
            </div>
          </div>

          {/* ── Step 2: 止盈止损 ── */}
          <div className="space-y-3 border-t border-slate-700/50 pt-4">
            <div className="flex items-center gap-2">
              <div className="w-7 h-7 rounded-lg bg-emerald-500/20 flex items-center justify-center text-xs font-bold text-emerald-400 shrink-0">2</div>
              <div>
                <div className="text-sm font-semibold text-slate-200">止盈止损配置</div>
                <div className="text-xs text-slate-500">KOL未提供时,系统自动分配</div>
              </div>
            </div>

            {/* 优先级链路 */}
            <div className="pl-9 flex items-center gap-1 flex-wrap text-xs">
              <span className="px-1.5 py-0.5 rounded bg-emerald-500/10 text-emerald-400">KOL信号</span>
              <span className="text-slate-600">›</span>
              <span className="px-1.5 py-0.5 rounded glass-soft text-slate-400">策略手动</span>
              <span className="text-slate-600">›</span>
              <span className="px-1.5 py-0.5 rounded glass-soft text-slate-400">币种分层</span>
              <span className="text-slate-600">›</span>
              <span className="px-1.5 py-0.5 rounded glass-soft text-slate-400">ATR修正</span>
              <span className="text-slate-600">›</span>
              <span className="px-1.5 py-0.5 rounded bg-amber-500/10 text-amber-400">超时保护</span>
            </div>

            {/* 分层参考表 */}
            <div className="pl-9 grid grid-cols-3 gap-2">
              <div className="glass-soft p-2 rounded-lg text-center border border-slate-700/50">
                <div className="text-xs font-medium text-slate-300">主流币 BTC/ETH</div>
                <div className="text-xs text-emerald-400 mt-1">SL 3% · TP 2/4/6%</div>
                <div className="text-xs text-slate-500">硬上限 8%</div>
              </div>
              <div className="glass-soft p-2 rounded-lg text-center border border-slate-700/50">
                <div className="text-xs font-medium text-slate-300">中型币 SOL/BNB</div>
                <div className="text-xs text-amber-400 mt-1">SL 5% · TP 3/6/10%</div>
                <div className="text-xs text-slate-500">硬上限 12%</div>
              </div>
              <div className="glass-soft p-2 rounded-lg text-center border border-slate-700/50">
                <div className="text-xs font-medium text-slate-300">山寨币</div>
                <div className="text-xs text-rose-400 mt-1">SL 8% · TP 5/10/15%</div>
                <div className="text-xs text-slate-500">硬上限 20%</div>
              </div>
            </div>

            {/* 分层开关 */}
            <label className="flex items-center gap-2 text-sm text-slate-300 pl-9 cursor-pointer">
              <input type="checkbox" className="accent-accent w-4 h-4" checked={f.use_tiered_defaults} onChange={(e) => setF({ ...f, use_tiered_defaults: e.target.checked })} />
              <span>使用币种分层默认</span>
              <span className="text-xs text-slate-500">按币种自动分配,无需手动填写</span>
            </label>

            {/* 手动配置 */}
            {!f.use_tiered_defaults && (
              <div className="grid grid-cols-2 gap-3 pl-9">
                <Field label="默认止损(%,负数)"><Input type="number" value={f.default_sl_pct} onChange={(e) => setF({ ...f, default_sl_pct: e.target.value })} /></Field>
                <Field label="止盈(%,逗号分隔)"><Input value={f.tp_levels} onChange={(e) => setF({ ...f, tp_levels: e.target.value })} /></Field>
              </div>
            )}

            {/* 硬止损 + 无止损 */}
            <div className="pl-9 grid grid-cols-2 gap-3">
              <Field label="硬止损上限(%, 0=分层默认)">
                <Input type="number" value={f.max_sl_pct} onChange={(e) => setF({ ...f, max_sl_pct: e.target.value })} />
              </Field>
              <div className="flex items-end pb-1">
                <label className="flex items-center gap-2 text-sm text-loss cursor-pointer">
                  <input type="checkbox" className="accent-loss w-4 h-4" checked={f.no_stop_loss} onChange={(e) => setF({ ...f, no_stop_loss: e.target.checked })} />
                  <span>无止损模式(高危)</span>
                </label>
              </div>
            </div>
            {Number(f.max_sl_pct) > 0 && f.use_tiered_defaults && (
              <div className="pl-9 text-xs text-amber-400">⚠ 自定义 {f.max_sl_pct}% 将覆盖分层硬上限 (8/12/20%)</div>
            )}
            {f.no_stop_loss && (
              <div className="pl-9 text-xs text-amber-400 bg-amber-950/30 border border-amber-800 rounded p-2">
                ⚠ 无止损模式仅使用硬止损上限兜底,适合高频网格策略
              </div>
            )}
          </div>

          {/* ── Step 3: 追踪止损 ── */}
          <div className="space-y-3 border-t border-slate-700/50 pt-4">
            <div className="flex items-center gap-2">
              <div className="w-7 h-7 rounded-lg bg-blue-500/20 flex items-center justify-center text-xs font-bold text-blue-400 shrink-0">3</div>
              <div>
                <div className="text-sm font-semibold text-slate-200">追踪止损</div>
                <div className="text-xs text-slate-500">盈利后自动切换为追踪模式</div>
              </div>
            </div>
            <label className="flex items-center gap-2 text-sm text-slate-300 pl-9 cursor-pointer">
              <input type="checkbox" className="accent-accent w-4 h-4" checked={f.enable_trailing} onChange={(e) => setF({ ...f, enable_trailing: e.target.checked })} />
              <span>启用追踪止损</span>
            </label>
            {f.enable_trailing && (
              <div className="pl-9 space-y-2">
                <div className="grid grid-cols-2 gap-3">
                  <Field label="追踪回撤(%)"><Input type="number" step="0.1" value={f.trailing_callback} onChange={(e) => setF({ ...f, trailing_callback: e.target.value })} /></Field>
                  <Field label="成本保护缓冲(%)"><Input type="number" step="0.1" value={f.cost_protection_buffer} onChange={(e) => setF({ ...f, cost_protection_buffer: e.target.value })} /></Field>
                </div>
                <div className="text-xs text-slate-500 flex items-center gap-1.5">
                  <span className="text-blue-400/60">▸</span>
                  开仓后先用分层/手动止损,盈利后自动切换追踪止损,超时保护 Phase1 将自动跳过
                </div>
              </div>
            )}
            {!f.enable_trailing && (
              <div className="pl-9 grid grid-cols-1 gap-3">
                <Field label="成本保护缓冲(%)"><Input type="number" step="0.1" value={f.cost_protection_buffer} onChange={(e) => setF({ ...f, cost_protection_buffer: e.target.value })} /></Field>
              </div>
            )}
          </div>

          {/* ── Step 4: 超时分级保护 ── */}
          <div className="space-y-3 border-t border-slate-700/50 pt-4">
            <div className="flex items-center gap-2">
              <div className="w-7 h-7 rounded-lg bg-amber-500/20 flex items-center justify-center text-xs font-bold text-amber-400 shrink-0">4</div>
              <div>
                <div className="text-sm font-semibold text-slate-200">超时分级保护</div>
                <div className="text-xs text-slate-500">仅对KOL未提供止盈止损的持仓生效</div>
              </div>
            </div>
            <label className="flex items-center gap-2 text-sm text-slate-300 pl-9 cursor-pointer">
              <input type="checkbox" className="accent-accent w-4 h-4" checked={f.timeout_protection_enabled} onChange={(e) => setF({ ...f, timeout_protection_enabled: e.target.checked })} />
              <span>启用超时分级保护</span>
            </label>
            {f.timeout_protection_enabled && (
              <div className="pl-9 space-y-2">
                <div className="grid grid-cols-4 gap-2">
                  <Field label="P1追踪(h)"><Input type="number" value={f.timeout_phase1_hours} onChange={(e) => setF({ ...f, timeout_phase1_hours: e.target.value })} /></Field>
                  <Field label="P2收紧(h)"><Input type="number" value={f.timeout_phase2_hours} onChange={(e) => setF({ ...f, timeout_phase2_hours: e.target.value })} /></Field>
                  <Field label="P3告警(h)"><Input type="number" value={f.timeout_phase3_hours} onChange={(e) => setF({ ...f, timeout_phase3_hours: e.target.value })} /></Field>
                  <Field label="P4平仓(h)"><Input type="number" value={f.timeout_phase4_hours} onChange={(e) => setF({ ...f, timeout_phase4_hours: e.target.value })} /></Field>
                </div>
                <div className="grid grid-cols-2 gap-2">
                  <Field label="P1回撤(%)"><Input type="number" step="0.5" value={f.timeout_trailing_p1} onChange={(e) => setF({ ...f, timeout_trailing_p1: e.target.value })} /></Field>
                  <Field label="P2回撤(%)"><Input type="number" step="0.5" value={f.timeout_trailing_p2} onChange={(e) => setF({ ...f, timeout_trailing_p2: e.target.value })} /></Field>
                </div>
                {/* 阶段说明卡片 */}
                <div className="grid grid-cols-4 gap-2">
                  <div className="glass-soft p-1.5 rounded text-center">
                    <div className="text-xs text-amber-400 font-medium">P1</div>
                    <div className="text-xs text-slate-500">启动追踪</div>
                  </div>
                  <div className="glass-soft p-1.5 rounded text-center">
                    <div className="text-xs text-amber-400 font-medium">P2</div>
                    <div className="text-xs text-slate-500">收紧回撤</div>
                  </div>
                  <div className="glass-soft p-1.5 rounded text-center">
                    <div className="text-xs text-rose-400 font-medium">P3</div>
                    <div className="text-xs text-slate-500">飞书告警</div>
                  </div>
                  <div className="glass-soft p-1.5 rounded text-center">
                    <div className="text-xs text-red-400 font-medium">P4</div>
                    <div className="text-xs text-slate-500">自动平仓</div>
                  </div>
                </div>
              </div>
            )}
          </div>

          {/* ── 保存 ── */}
          <div className="border-t border-slate-700/50 pt-4">
            <Button className="w-full" onClick={save}>保存策略</Button>
          </div>
        </div>
      </Modal>
    </div>
  );
}
