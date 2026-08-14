import { useEffect, useState } from "react";
import { Radio, Filter, AlertTriangle, CheckCircle2, XCircle, Clock, Send, Activity } from "lucide-react";
import { API } from "@/api/client";
import { useFetch } from "@/lib/useFetch";
import { useDebouncedReload } from "@/lib/useDebouncedReload";
import { useToast } from "@/components/ui/Toast";
import { wsClient } from "@/api/ws";
import { useAuthStore } from "@/stores/auth";
import { Card, CardTitle, Badge, Button, Select, Empty, Input, Field } from "@/components/ui";
import { Modal } from "@/components/ui/Modal";
import { fmtTime, fmtMoney, sideLabel, signalStatusLabel } from "@/lib/utils";

const STATUS_TONE: Record<string, any> = {
  received: "default",
  parsed: "accent",
  filtered: "warn",
  corrected: "warn",
  ordered: "profit",
  rejected: "loss",
  ignored: "default",
  no_followers: "warn",
};

const STATUS_ICON: Record<string, any> = {
  ordered: CheckCircle2,
  rejected: XCircle,
  corrected: AlertTriangle,
  filtered: Filter,
  received: Clock,
  parsed: Radio,
  ignored: Clock,
  no_followers: AlertTriangle,
};

const STATUS_FILTERS = [
  { k: "", label: "全部" },
  { k: "ordered", label: "已下单" },
  { k: "corrected", label: "已纠错" },
  { k: "rejected", label: "已拒绝" },
  { k: "filtered", label: "已过滤" },
  { k: "received", label: "已接收" },
  { k: "no_followers", label: "未订阅" },
  { k: "ignored", label: "已忽略" },
];

export default function SignalsPage() {
  const { user } = useAuthStore();
  const isAdmin = user?.role === "admin";
  const [status, setStatus] = useState("");
  const [page, setPage] = useState(1);
  const pageSize = 50;
  const { data, reload } = useFetch(() => API.listSignals(page, pageSize, undefined, status || undefined), [page, status]);
  const debouncedReload = useDebouncedReload(reload, 600);
  const { push } = useToast();

  const { data: kolsData } = useFetch(() => (isAdmin ? API.listAdminKols() : Promise.resolve([])), []);
  const [inject, setInject] = useState(false);
  const [f, setF] = useState({ kol_id: "", raw_text: "$SOL long entry 150 TP 155 160 SL 145 lev 5x" });

  useEffect(() => {
    setPage(1);
  }, [status]);

  useEffect(() => {
    const off = wsClient.on((event) => {
      if (event === "signal") debouncedReload();
    });
    return () => {
      off();
    };
  }, [debouncedReload]);

  const res: any = data || {};
  const items: any[] = res.items || [];
  const hasPrev = page > 1;
  const hasNext = items.length >= pageSize;

  const statusCounts = items.reduce((acc: any, sig: any) => {
    acc[sig.status] = (acc[sig.status] || 0) + 1;
    return acc;
  }, {});

  const kols: any[] = kolsData || [];

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
    <div className="space-y-4 md:space-y-6">
      <div className="flex flex-col sm:flex-row sm:items-center justify-between gap-3">
        <div>
          <h1 className="text-xl font-bold gradient-text flex items-center gap-2">
            信号汇总
            <Badge tone="accent"><Activity size={12} className="animate-pulse" /> 实时</Badge>
          </h1>
          <p className="text-sm text-slate-500 mt-1">
            {isAdmin ? "全局信号处理监控 · 可手动注入测试信号" : "所有 KOL 信号及处理轨迹(去重/纠错/下单/拒绝)"}
          </p>
        </div>
        <div className="flex gap-2 flex-wrap">
          {isAdmin && (
            <Button onClick={() => setInject(true)}><Send size={15} /> 注入测试信号</Button>
          )}
        </div>
      </div>

      {isAdmin && (
        <div className="grid grid-cols-2 md:grid-cols-4 gap-3">
          <div className="glass p-4">
            <div className="text-xs text-slate-500 mb-1">本页信号</div>
            <div className="text-xl md:text-2xl font-bold font-mono text-slate-100">{items.length}</div>
          </div>
          <div className="glass p-4">
            <div className="text-xs text-slate-500 mb-1">已下单</div>
            <div className="text-xl md:text-2xl font-bold font-mono text-profit">{statusCounts.ordered || 0}</div>
          </div>
          <div className="glass p-4">
            <div className="text-xs text-slate-500 mb-1">已拒绝/过滤/未订阅</div>
            <div className="text-xl md:text-2xl font-bold font-mono text-loss">
              {(statusCounts.rejected || 0) + (statusCounts.filtered || 0) + (statusCounts.no_followers || 0)}
            </div>
          </div>
          <div className="glass p-4">
            <div className="text-xs text-slate-500 mb-1">已纠错</div>
            <div className="text-xl md:text-2xl font-bold font-mono text-warn">{statusCounts.corrected || 0}</div>
          </div>
        </div>
      )}

      <div className="flex gap-2 flex-wrap">
        {STATUS_FILTERS.map((s) => (
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
      </div>

      <Card>
        <CardTitle
          action={
            <div className="flex items-center gap-2 flex-wrap justify-end">
              <span className="font-mono text-xs text-slate-500">第 {page} 页</span>
              <Button variant="ghost" className="px-2 py-1 text-xs" disabled={!hasPrev} onClick={() => setPage((p) => Math.max(1, p - 1))}>
                上一页
              </Button>
              <Button variant="ghost" className="px-2 py-1 text-xs" disabled={!hasNext} onClick={() => setPage((p) => p + 1)}>
                下一页
              </Button>
            </div>
          }
        >
          信号流({items.length})
        </CardTitle>
        {items.length === 0 ? (
          <Empty text="暂无信号" />
        ) : (
          <div className="space-y-2.5">
            {items.map((sig) => {
              const Icon = STATUS_ICON[sig.status] || Radio;
              const p = sig.parsed || {};
              return (
                <div
                  key={sig.id}
                  className={`glass-soft p-3.5 md:p-4 relative overflow-hidden border-l-2 ${
                    sig.status === "ordered"
                      ? "border-l-profit"
                      : sig.status === "rejected"
                      ? "border-l-loss"
                      : sig.status === "corrected"
                      ? "border-l-warn"
                      : "border-l-border-soft"
                  }`}
                >
                  <div className="flex items-center gap-2 md:gap-3 flex-wrap">
                    <div className="flex items-center gap-2 min-w-0">
                      <div className="w-7 h-7 md:w-8 md:h-8 rounded-lg bg-accent/15 flex items-center justify-center text-accent-glow text-[10px] md:text-xs font-bold shrink-0">
                        {sig.kol_name?.[0] || "K"}
                      </div>
                      <span className="text-sm font-semibold text-slate-100 truncate">{sig.kol_name}</span>
                    </div>
                    {sig.symbol && <Badge tone="accent" className="text-[10px]">{sig.symbol}</Badge>}
                    {p.side && <Badge tone={p.side === "long" ? "profit" : "loss"} className="text-[10px]">{sideLabel(p.side)}</Badge>}
                    {p.entry_price && (
                      <span className="text-xs text-slate-400 font-mono hidden sm:inline">@ {fmtMoney(p.entry_price, 4)}</span>
                    )}
                    {p.leverage > 1 && <Badge className="text-[10px]">{p.leverage}x</Badge>}
                    <Badge tone={STATUS_TONE[sig.status] || "default"} className="text-[10px]">
                      <Icon size={11} /> {signalStatusLabel(sig.status)}
                    </Badge>
                    {sig.corrected && (
                      <Badge tone="warn" className="text-[10px]"><AlertTriangle size={11} /> 已纠错</Badge>
                    )}
                    {sig.confidence > 0 && (
                      <span className="text-[11px] text-slate-500 hidden sm:inline">置信度 {(sig.confidence * 100).toFixed(0)}%</span>
                    )}
                    <span className="text-[11px] text-slate-600 ml-auto">{fmtTime(sig.received_at)}</span>
                  </div>
                  <div className="text-xs md:text-sm text-slate-400 mt-2 font-mono whitespace-pre-wrap break-all line-clamp-2 md:line-clamp-3">
                    {sig.raw_text?.slice(0, 240) || "(无文本)"}
                  </div>
                  {sig.image_url && (
                    <a href={sig.image_url} target="_blank" className="text-xs text-accent-glow mt-1 inline-block">
                      查看图片
                    </a>
                  )}
                  {(p.take_profits?.length || p.stop_loss) && (
                    <div className="flex gap-3 mt-2 text-[11px] md:text-xs flex-wrap">
                      {p.take_profits?.length > 0 && (
                        <span className="text-profit">TP: {p.take_profits.map((t: number) => fmtMoney(t, 4)).join(" / ")}</span>
                      )}
                      {p.stop_loss && <span className="text-loss">SL: {fmtMoney(p.stop_loss, 4)}</span>}
                    </div>
                  )}
                  {sig.correct_log && (
                    <div className="mt-2 text-xs bg-warn/10 border border-warn/20 rounded-lg p-2 text-warn">
                      <AlertTriangle size={11} className="inline mr-1" />
                      纠错轨迹: {sig.correct_log}
                    </div>
                  )}
                  {sig.note && <div className="mt-1.5 text-xs text-slate-500">{sig.note}</div>}
                </div>
              );
            })}
          </div>
        )}
      </Card>

      {isAdmin && (
        <Modal open={inject} onClose={() => setInject(false)} title="注入测试信号">
          <div className="space-y-4">
            <Field label="选择 KOL">
              <Select value={f.kol_id} onChange={(e) => setF({ ...f, kol_id: e.target.value })}>
                <option value="">请选择</option>
                {kols.map((k) => (
                  <option key={k.id} value={k.id}>{k.name} ({k.discord_channel_id})</option>
                ))}
              </Select>
            </Field>
            <Field label="信号文本(模拟 KOL 消息)">
              <Input value={f.raw_text} onChange={(e) => setF({ ...f, raw_text: e.target.value })} />
            </Field>
            <div className="text-xs text-slate-500 bg-bg-soft rounded-lg p-3">
              示例: <code className="text-accent-glow">$SOL long entry 150 TP 155 160 SL 145 lev 5x</code>。注入后会走完整解析 → 过滤 → 对关注该 KOL 的客户下单流程。
            </div>
            <Button className="w-full" onClick={doInject}>注入</Button>
          </div>
        </Modal>
      )}
    </div>
  );
}

