import { useEffect, useState } from "react";
import { Radio, Send, Activity } from "lucide-react";
import { API } from "@/api/client";
import { useFetch } from "@/lib/useFetch";
import { useToast } from "@/components/ui/Toast";
import { wsClient } from "@/api/ws";
import { Card, CardTitle, Badge, Button, Empty, Select, Input, Field } from "@/components/ui";
import { Modal } from "@/components/ui/Modal";
import { fmtTime, fmtMoney, sideLabel, signalStatusLabel } from "@/lib/utils";

export default function AdminSignals() {
  const [status, setStatus] = useState("");
  const { data, reload } = useFetch(() => API.listSignals(1, 100, undefined, status || undefined), [status]);
  const { data: kolsData } = useFetch(() => API.listAdminKols(), []);
  const { push } = useToast();
  const [inject, setInject] = useState(false);
  const [f, setF] = useState({ kol_id: "", raw_text: "$SOL long entry 150 TP 155 160 SL 145 lev 5x" });

  useEffect(() => {
    const off = wsClient.on((event) => { if (event === "signal") reload(); });
    return () => {
      off();
    };
  }, [reload]);

  const kols: any[] = kolsData || [];
  const res: any = data || {};
  const items: any[] = res.items || [];

  const statusCounts = items.reduce((acc: any, sig: any) => {
    acc[sig.status] = (acc[sig.status] || 0) + 1;
    return acc;
  }, {});

  const doInject = async () => {
    try {
      await API.injectSignal({ kol_id: Number(f.kol_id), raw_text: f.raw_text });
      push("success", "信号已注入(模拟 KOL 消息)");
      setInject(false);
      reload();
    } catch (e: any) {
      push("error", e?.response?.data?.message || "注入失败");
    }
  };

  return (
    <div className="space-y-6">
      <div className="flex items-center justify-between">
        <div>
          <h1 className="text-xl font-bold gradient-text flex items-center gap-2">信号监控</h1>
          <p className="text-sm text-slate-500 mt-1">全局信号处理监控 · 可手动注入测试信号</p>
        </div>
        <div className="flex gap-2 flex-wrap">
          {[
            { k: "", label: "全部" },
            { k: "ordered", label: "已下单" },
            { k: "corrected", label: "已纠错" },
            { k: "rejected", label: "已拒绝" },
            { k: "filtered", label: "已过滤" },
          ].map((s) => (
            <button
              key={s.k}
              onClick={() => setStatus(s.k)}
              className={`px-3 py-1.5 rounded-lg text-xs font-semibold transition-all ${
                status === s.k
                  ? "bg-gold/15 text-gold border border-gold/50 shadow-[0_0_12px_-2px_rgba(245,158,11,0.2)]"
                  : "glass-soft border border-border-soft text-slate-400 hover:text-slate-200"
              }`}
            >
              {s.label}
            </button>
          ))}
          <Button onClick={() => setInject(true)}><Send size={15} /> 注入测试信号</Button>
        </div>
      </div>

      {/* KPI 概览 */}
      <div className="grid grid-cols-2 md:grid-cols-4 gap-3">
        <div className="glass p-4"><div className="text-xs text-slate-500 mb-1">信号总数</div><div className="text-xl md:text-2xl font-bold font-mono text-slate-100">{items.length}</div></div>
        <div className="glass p-4"><div className="text-xs text-slate-500 mb-1">已下单</div><div className="text-xl md:text-2xl font-bold font-mono text-profit">{statusCounts.ordered || 0}</div></div>
        <div className="glass p-4"><div className="text-xs text-slate-500 mb-1">已拒绝/过滤</div><div className="text-xl md:text-2xl font-bold font-mono text-loss">{(statusCounts.rejected || 0) + (statusCounts.filtered || 0)}</div></div>
        <div className="glass p-4"><div className="text-xs text-slate-500 mb-1">已纠错</div><div className="text-xl md:text-2xl font-bold font-mono text-warn">{statusCounts.corrected || 0}</div></div>
      </div>

      <Card>
        <CardTitle action={<Badge tone="accent"><Activity size={12} className="animate-pulse" /> 实时</Badge>}>
          信号流({items.length})
        </CardTitle>
        {items.length === 0 ? (
          <Empty text="暂无信号" />
        ) : (
          <div className="space-y-2">
            {items.map((sig) => {
              const p = sig.parsed || {};
              return (
                <div key={sig.id} className={`glass-soft p-3 relative overflow-hidden border-l-2 ${sig.status === "ordered" ? "border-l-profit" : sig.status === "rejected" ? "border-l-loss" : sig.status === "corrected" ? "border-l-warn" : "border-l-border-soft"}`}>
                  <div className="flex items-center gap-2 flex-wrap text-sm">
                    <span className="font-medium text-slate-100">{sig.kol_name}</span>
                    {p.symbol && <Badge tone="accent">{p.symbol}</Badge>}
                    {p.side && <Badge tone={p.side === "long" ? "profit" : "loss"}>{sideLabel(p.side)}</Badge>}
                    <Badge tone={sig.status === "ordered" ? "profit" : sig.status === "rejected" ? "loss" : sig.corrected ? "warn" : "default"}>{signalStatusLabel(sig.status)}</Badge>
                    {sig.corrected && <Badge tone="warn">已纠错</Badge>}
                    <span className="text-xs text-slate-600 ml-auto">{fmtTime(sig.received_at)}</span>
                  </div>
                  <div className="text-xs text-slate-400 mt-1 font-mono break-all">{sig.raw_text?.slice(0, 200)}</div>
                  {sig.correct_log && <div className="text-xs text-warn mt-1">⚠ {sig.correct_log}</div>}
                  {sig.note && <div className="text-xs text-slate-500 mt-1">{sig.note}</div>}
                </div>
              );
            })}
          </div>
        )}
      </Card>

      <Modal open={inject} onClose={() => setInject(false)} title="注入测试信号">
        <div className="space-y-4">
          <Field label="选择 KOL">
            <Select value={f.kol_id} onChange={(e) => setF({ ...f, kol_id: e.target.value })}>
              <option value="">请选择</option>
              {kols.map((k) => <option key={k.id} value={k.id}>{k.name} ({k.discord_channel_id})</option>)}
            </Select>
          </Field>
          <Field label="信号文本(模拟 KOL 消息)">
            <Input value={f.raw_text} onChange={(e) => setF({ ...f, raw_text: e.target.value })} />
          </Field>
          <div className="text-xs text-slate-500 bg-bg-soft rounded-lg p-3">
            示例:`$SOL long entry 150 TP 155 160 SL 145 lev 5x`。注入后会走完整解析→过滤→对关注该 KOL 的客户下单流程。
          </div>
          <Button className="w-full" onClick={doInject}>注入</Button>
        </div>
      </Modal>
    </div>
  );
}
