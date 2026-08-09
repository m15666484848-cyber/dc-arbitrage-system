import { useEffect, useState } from "react";
import { Radio, Filter, AlertTriangle, CheckCircle2, XCircle, Clock } from "lucide-react";
import { API } from "@/api/client";
import { useFetch } from "@/lib/useFetch";
import { wsClient } from "@/api/ws";
import { Card, CardTitle, Badge, Select, Empty } from "@/components/ui";
import { fmtTime, fmtMoney, sideLabel, signalStatusLabel } from "@/lib/utils";

const STATUS_TONE: Record<string, any> = {
  received: "default",
  parsed: "accent",
  filtered: "warn",
  corrected: "warn",
  ordered: "profit",
  rejected: "loss",
  ignored: "default",
};

const STATUS_ICON: Record<string, any> = {
  ordered: CheckCircle2,
  rejected: XCircle,
  corrected: AlertTriangle,
  filtered: Filter,
  received: Clock,
  parsed: Radio,
  ignored: Clock,
};

export default function SignalsPage() {
  const [status, setStatus] = useState("");
  const { data, reload } = useFetch(() => API.listSignals(1, 100, undefined, status || undefined), [status]);

  useEffect(() => {
    const off = wsClient.on((event) => {
      if (event === "signal") reload();
    });
    return () => {
      off();
    };
  }, [reload]);

  const res: any = data || {};
  const items: any[] = res.items || [];

  return (
    <div className="space-y-4 md:space-y-6">
      <div className="flex flex-col sm:flex-row sm:items-center justify-between gap-3">
        <div>
          <h1 className="text-xl font-bold gradient-text flex items-center gap-2">信号汇总</h1>
          <p className="text-sm text-slate-500 mt-1">所有 KOL 信号及处理轨迹(去重/纠错/下单/拒绝)</p>
        </div>
        <Select value={status} onChange={(e) => setStatus(e.target.value)} className="w-full sm:w-44">
          <option value="">全部状态</option>
          <option value="received">已接收</option>
          <option value="ordered">已下单</option>
          <option value="corrected">已纠错</option>
          <option value="filtered">已过滤</option>
          <option value="rejected">已拒绝</option>
          <option value="ignored">已忽略</option>
        </Select>
      </div>

      <Card>
        {items.length === 0 ? (
          <Empty text="暂无信号" />
        ) : (
          <div className="space-y-2.5">
            {items.map((sig) => {
              const Icon = STATUS_ICON[sig.status] || Radio;
              const p = sig.parsed || {};
              return (
                <div key={sig.id} className="glass-soft p-3.5 md:p-4">
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
                    {sig.corrected && <Badge tone="warn" className="text-[10px]"><AlertTriangle size={11} /> 已纠错</Badge>}
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
                      📎 查看图片
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
                      纠错轨迹:{sig.correct_log}
                    </div>
                  )}
                  {sig.note && (
                    <div className="mt-1.5 text-xs text-slate-500">{sig.note}</div>
                  )}
                </div>
              );
            })}
          </div>
        )}
      </Card>
    </div>
  );
}
