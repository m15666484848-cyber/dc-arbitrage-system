import { useState } from "react";
import { Gauge, Plus, Pencil, Trash2, ToggleLeft, ToggleRight } from "lucide-react";
import { API } from "@/api/client";
import { useFetch } from "@/lib/useFetch";
import { useToast } from "@/components/ui/Toast";
import { Button, Input, Select, Field, Card, Badge } from "@/components/ui";
import { Modal } from "@/components/ui/Modal";

interface SymbolNotional {
  id: number;
  name: string;
  symbols: string;
  multiplier: number;
  enabled: boolean;
  note: string;
}

const EMPTY: Omit<SymbolNotional, "id"> = {
  name: "",
  symbols: "",
  multiplier: 1.0,
  enabled: true,
  note: "",
};

const PRESETS = [
  { name: "主流币", symbols: "BTC,ETH", multiplier: 0.5, note: "波动小,下单量减半" },
  { name: "贵金属", symbols: "XAU,XAG", multiplier: 1.0, note: "黄金白银" },
  { name: "能源", symbols: "XBR,XTI", multiplier: 1.0, note: "原油天然气" },
  { name: "山寨币", symbols: "SOL,DOGE,PEPE", multiplier: 2.0, note: "波动大,风险高" },
  { name: "指数", symbols: "NAS100,US30", multiplier: 0.8, note: "指数类品种" },
];

export default function SymbolNotionalPage() {
  const { data, reload } = useFetch(() => API.listSymbolNotional(), []);
  const { push } = useToast();
  const [modal, setModal] = useState(false);
  const [editId, setEditId] = useState<number | null>(null);
  const [f, setF] = useState<any>({ ...EMPTY });

  const list: SymbolNotional[] = data || [];

  const open = (cfg?: SymbolNotional) => {
    if (cfg) {
      setEditId(cfg.id);
      setF({ ...cfg });
    } else {
      setEditId(null);
      setF({ ...EMPTY });
    }
    setModal(true);
  };

  const save = async () => {
    try {
      if (!f.name?.trim()) {
        push("error", "请填写分类名称");
        return;
      }
      if (!f.symbols?.trim()) {
        push("error", "请填写品种代码");
        return;
      }
      if (editId) {
        await API.updateSymbolNotional(editId, f);
        push("success", "已更新");
      } else {
        await API.createSymbolNotional(f);
        push("success", "已创建");
      }
      setModal(false);
      reload();
    } catch (e: any) {
      push("error", e?.response?.data?.message || "保存失败");
    }
  };

  const remove = async (id: number) => {
    if (!confirm("确定删除此分类?")) return;
    try {
      await API.deleteSymbolNotional(id);
      push("success", "已删除");
      reload();
    } catch (e: any) {
      push("error", e?.response?.data?.message || "删除失败");
    }
  };

  const toggle = async (id: number, enabled: boolean) => {
    try {
      await API.updateSymbolNotional(id, { enabled });
      reload();
    } catch (e: any) {
      push("error", e?.response?.data?.message || "更新失败");
    }
  };

  const applyPreset = (p: typeof PRESETS[0]) => {
    setEditId(null);
    setF({ ...EMPTY, name: p.name, symbols: p.symbols, multiplier: p.multiplier, enabled: true, note: p.note });
    setModal(true);
  };

  return (
    <div className="space-y-4 md:space-y-6">
      <div className="flex flex-col sm:flex-row sm:items-center justify-between gap-3">
        <div>
          <h1 className="text-xl font-bold gradient-text flex items-center gap-2">品种分类倍率</h1>
          <p className="text-sm text-slate-500 mt-1">按品种分类设置跟单金额倍率,匹配品种前缀时自动应用</p>
        </div>
        <div className="flex gap-2 flex-wrap">
          {PRESETS.map((p) => (
            <button
              key={p.name}
              onClick={() => applyPreset(p)}
              className="text-xs px-3 py-1.5 rounded-lg glass border border-border-soft hover:border-gold hover:text-gold transition"
              title={p.note}
            >
              + {p.name} ({p.multiplier}x)
            </button>
          ))}
          <Button onClick={() => open()}>
            <Plus size={14} /> 新建分类
          </Button>
        </div>
      </div>

      <Card>
        <div className="overflow-x-auto">
          <table className="w-full text-sm">
            <thead>
              <tr className="text-left text-slate-500 border-b border-border-soft">
                <th className="pb-2 pr-3 font-medium">分类名</th>
                <th className="pb-2 pr-3 font-medium">品种前缀</th>
                <th className="pb-2 pr-3 font-medium">倍率</th>
                <th className="pb-2 pr-3 font-medium">状态</th>
                <th className="pb-2 pr-3 font-medium">备注</th>
                <th className="pb-2 font-medium text-right">操作</th>
              </tr>
            </thead>
            <tbody>
              {list.length === 0 ? (
                <tr>
                  <td colSpan={6} className="py-8 text-center text-slate-500">暂无分类,点击上方预设快速创建</td>
                </tr>
              ) : (
                list.map((r) => (
                  <tr key={r.id} className="border-b border-border-soft/50 hover:bg-bg-hover/30">
                    <td className="py-2.5 pr-3 font-medium text-slate-200">{r.name}</td>
                    <td className="py-2.5 pr-3">
                      <div className="flex gap-1 flex-wrap">
                        {r.symbols.split(",").filter(Boolean).map((s) => (
                          <Badge key={s} tone="accent" className="!py-0 !px-1.5 !text-[10px]">{s.trim()}</Badge>
                        ))}
                      </div>
                    </td>
                    <td className="py-2.5 pr-3">
                      <span className={`font-mono font-bold ${r.multiplier < 1 ? "text-warn" : r.multiplier > 1 ? "text-accent-glow" : "text-slate-300"}`}>
                        {r.multiplier}x
                      </span>
                    </td>
                    <td className="py-2.5 pr-3">
                      {r.enabled ? (
                        <Badge tone="accent" className="!py-0 !px-1.5 !text-[10px]">启用</Badge>
                      ) : (
                        <Badge tone="default" className="!py-0 !px-1.5 !text-[10px]">禁用</Badge>
                      )}
                    </td>
                    <td className="py-2.5 pr-3 text-slate-500 text-xs max-w-[200px] truncate">{r.note || "—"}</td>
                    <td className="py-2.5 text-right">
                      <div className="flex justify-end gap-1">
                        <button onClick={() => toggle(r.id, !r.enabled)} className="p-1 text-slate-400 hover:text-accent-glow" title={r.enabled ? "禁用" : "启用"}>
                          {r.enabled ? <ToggleRight size={16} /> : <ToggleLeft size={16} />}
                        </button>
                        <button onClick={() => open(r)} className="p-1 text-slate-400 hover:text-accent-glow" title="编辑">
                          <Pencil size={14} />
                        </button>
                        <button onClick={() => remove(r.id)} className="p-1 text-slate-400 hover:text-danger" title="删除">
                          <Trash2 size={14} />
                        </button>
                      </div>
                    </td>
                  </tr>
                ))
              )}
            </tbody>
          </table>
        </div>
      </Card>

      <div className="glass p-4 rounded-xl border border-border-soft">
        <h3 className="text-sm font-semibold text-slate-300 mb-2">使用说明</h3>
        <ul className="text-xs text-slate-500 space-y-1 list-disc list-inside">
          <li><span className="text-slate-400">品种前缀</span>:用逗号分隔,匹配规则为 symbol 代码前缀匹配(不区分大小写)。例如 <code className="text-accent-glow">BTC</code> 可匹配 BTC/USDT、BTCUSDT 等</li>
          <li><span className="text-slate-400">倍率</span>:最终下单金额 = 策略/客户设置金额 × 倍率。0.5x = 减半,2.0x = 翻倍</li>
          <li><span className="text-slate-400">匹配优先级</span>:按创建顺序从上到下匹配,第一个匹配到的分类生效</li>
          <li>未匹配任何分类的品种使用默认倍率 1.0(即不调整)</li>
        </ul>
      </div>

      <Modal open={modal} onClose={() => setModal(false)} title={editId ? "编辑分类" : "新建分类"} width="max-w-md">
        <div className="space-y-4">
          <Field label="分类名称">
            <Input value={f.name} onChange={(e) => setF({ ...f, name: e.target.value })} placeholder="如: 主流币" />
          </Field>
          <Field label="品种前缀 (逗号分隔)">
            <Input value={f.symbols} onChange={(e) => setF({ ...f, symbols: e.target.value })} placeholder="BTC, ETH" />
          </Field>
          <Field label={`倍率 (当前: ${f.multiplier}x)`}>
            <Input
              type="number"
              step="0.1"
              min="0.01"
              value={f.multiplier}
              onChange={(e) => setF({ ...f, multiplier: parseFloat(e.target.value) || 1.0 })}
            />
          </Field>
          <div className="flex items-center gap-2 text-sm">
            <input
              type="checkbox"
              id="enabled"
              checked={f.enabled}
              onChange={(e) => setF({ ...f, enabled: e.target.checked })}
              className="accent-accent-glow"
            />
            <label htmlFor="enabled" className="text-slate-400 cursor-pointer">启用</label>
          </div>
          <Field label="备注">
            <Input value={f.note} onChange={(e) => setF({ ...f, note: e.target.value })} placeholder="可选" />
          </Field>
          <div className="p-3 rounded-lg bg-slate-800/50 text-xs text-slate-400">
            预览: <span className="font-mono text-accent-glow">100 USDT</span> × <span className="font-mono text-accent-glow">{f.multiplier}x</span> = <span className="font-mono font-bold text-slate-100">{(100 * (f.multiplier || 1)).toFixed(2)} USDT</span>
          </div>
          <Button className="w-full" onClick={save}>保存</Button>
        </div>
      </Modal>
    </div>
  );
}