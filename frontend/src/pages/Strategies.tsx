import { useState } from "react";
import { SlidersHorizontal, Plus, Pencil, Trash2 } from "lucide-react";
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
};

export default function StrategiesPage() {
  const { data, reload } = useFetch(() => API.listStrategies(), []);
  const { push } = useToast();
  const [modal, setModal] = useState(false);
  const [editId, setEditId] = useState<number | null>(null);
  const [f, setF] = useState<any>({ ...EMPTY });

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
      default_sl_pct: Number(f.default_sl_pct) / 100,
      cost_protection_buffer: Number(f.cost_protection_buffer) / 100,
      enable_trailing: f.enable_trailing,
      trailing_callback: Number(f.trailing_callback) / 100,
      no_stop_loss: f.no_stop_loss,
      tp_levels: f.tp_levels.split(",").map((s: string) => Number(s.trim())),
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
    try {
      await API.deleteStrategy(id);
      push("success", "策略已删除");
      reload();
    } catch (e: any) {
      push("error", "删除失败");
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

      <div className="grid md:grid-cols-2 xl:grid-cols-3 gap-4">
        {list.length === 0 ? (
          <div className="col-span-full"><Card><Empty text="暂无策略,点击右上角新建" /></Card></div>
        ) : (
          list.map((s) => {
            const t = TYPES.find((x) => x.value === s.type);
            const p = s.params || {};
            return (
              <Card key={s.id} className={`relative overflow-hidden border-l-4 ${s.type === "martingale" ? "border-l-loss" : s.type === "anti_martingale" ? "border-l-accent-glow" : "border-l-profit"}`}>
                <div className="flex items-start justify-between">
                  <div>
                    <div className="flex items-center gap-2">
                      <SlidersHorizontal size={15} className="text-accent-glow" />
                      <span className="font-semibold text-slate-100">{s.name}</span>
                    </div>
                    <Badge tone="accent" className="mt-2">{t?.label}</Badge>
                  </div>
                  <div className="flex gap-1">
                    <button onClick={() => open(s)} className="text-slate-400 hover:text-accent-glow p-1"><Pencil size={14} /></button>
                    <button onClick={() => remove(s.id)} className="text-slate-400 hover:text-loss p-1"><Trash2 size={14} /></button>
                  </div>
                </div>
                <p className="text-xs text-slate-500 mt-2">{t?.desc}</p>
                <div className="grid grid-cols-2 gap-2 mt-3 text-xs">
                  <div className="glass-soft p-2"><div className="text-slate-500">基础仓位</div><div className="font-mono text-slate-100">{p.base_qty} U</div></div>
                  <div className="glass-soft p-2"><div className="text-slate-500">成本保护</div><div className="font-mono text-slate-100">{((p.cost_protection_buffer ?? 0.02) * 100).toFixed(1)}%</div></div>
                  {s.type !== "normal" && (
                    <>
                      <div className="glass-soft p-2"><div className="text-slate-500">马丁倍数</div><div className="font-mono text-slate-100">{p.martingale_multiplier}x</div></div>
                      <div className="glass-soft p-2"><div className="text-slate-500">熔断轮数</div><div className="font-mono text-slate-100">{p.max_rounds}</div></div>
                    </>
                  )}
                </div>
                {s.type !== "normal" && (
                  <div className="text-xs text-slate-400 mt-2">当前轮次:{s.martingale_round} · 上次:{s.last_result || "—"}</div>
                )}
              </Card>
            );
          })
        )}
      </div>

      <Modal open={modal} onClose={() => setModal(false)} title={editId ? "编辑策略" : "新建策略"} width="max-w-xl">
        <div className="space-y-4">
          <div className="grid grid-cols-2 gap-3">
            <Field label="策略名称"><Input value={f.name} onChange={(e) => setF({ ...f, name: e.target.value })} /></Field>
            <Field label="策略类型">
              <Select value={f.type} onChange={(e) => setF({ ...f, type: e.target.value })}>
                {TYPES.map((t) => <option key={t.value} value={t.value}>{t.label}</option>)}
              </Select>
            </Field>
          </div>
          <div className="grid grid-cols-3 gap-3">
            <Field label="基础仓位(USDT)"><Input type="number" value={f.base_qty} onChange={(e) => setF({ ...f, base_qty: e.target.value })} /></Field>
            {f.type !== "normal" && (
              <>
                <Field label="马丁倍数"><Input type="number" step="0.1" value={f.martingale_multiplier} onChange={(e) => setF({ ...f, martingale_multiplier: e.target.value })} /></Field>
                <Field label="熔断轮数"><Input type="number" value={f.max_rounds} onChange={(e) => setF({ ...f, max_rounds: e.target.value })} /></Field>
              </>
            )}
          </div>
          <div className="grid grid-cols-2 gap-3">
            <Field label="默认止损涨幅(%)"><Input type="number" value={f.default_sl_pct} onChange={(e) => setF({ ...f, default_sl_pct: e.target.value })} /></Field>
          </div>
          <Field label="止盈涨幅(%,逗号分隔,如 10,20,30 表示涨10%/20%/30%止盈)">
            <Input value={f.tp_levels} onChange={(e) => setF({ ...f, tp_levels: e.target.value })} />
          </Field>
          <div className="grid grid-cols-2 gap-3">
            <Field label="成本保护缓冲(%)"><Input type="number" step="0.1" value={f.cost_protection_buffer} onChange={(e) => setF({ ...f, cost_protection_buffer: e.target.value })} /></Field>
            <Field label="追踪止损回撤(%)"><Input type="number" step="0.1" value={f.trailing_callback} onChange={(e) => setF({ ...f, trailing_callback: e.target.value })} /></Field>
          </div>
          <label className="flex items-center gap-2 text-sm text-slate-300">
            <input type="checkbox" className="accent-accent" checked={f.enable_trailing} onChange={(e) => setF({ ...f, enable_trailing: e.target.checked })} />
            启用追踪止损
          </label>
          <label className="flex items-center gap-2 text-sm text-loss">
            <input type="checkbox" className="accent-loss" checked={f.no_stop_loss} onChange={(e) => setF({ ...f, no_stop_loss: e.target.checked })} />
            无止损模式(高危,慎选)
          </label>
          <Button className="w-full" onClick={save}>保存策略</Button>
        </div>
      </Modal>
    </div>
  );
}
