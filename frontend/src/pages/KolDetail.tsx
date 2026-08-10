import { Link, useParams } from "react-router-dom";
import { ArrowLeft, Radio, Trophy, Percent } from "lucide-react";
import { API } from "@/api/client";
import { useFetch } from "@/lib/useFetch";
import { Card, CardTitle, Badge, Empty, Stat } from "@/components/ui";
import { fmtMoney, fmtTime } from "@/lib/utils";

export default function KolDetailPage() {
  const { id } = useParams();
  const kolId = Number(id);
  const { data: kolsData } = useFetch(() => API.listKols(), []);
  const { data: rankingData } = useFetch(() => API.kolRanking(30), []);
  const { data: signalsData } = useFetch(() => API.listSignals(1, 50, kolId), [kolId]);

  const kols: any[] = kolsData || [];
  const ranking: any[] = rankingData || [];
  const kol = kols.find((k) => k.id === kolId);
  const rank = ranking.find((r) => r.kol_id === kolId) || {};
  const signals: any[] = signalsData?.items || [];

  return (
    <div className="space-y-4 md:space-y-6">
      <div className="flex items-center justify-between gap-3">
        <div>
          <Link to="/kols" className="text-xs text-slate-500 hover:text-accent-glow inline-flex items-center gap-1 mb-2">
            <ArrowLeft size={13} /> 返回 KOL 列表
          </Link>
          <h1 className="text-xl font-bold text-slate-100">{kol?.name || rank.kol_name || "KOL 详情"}</h1>
          <p className="text-sm text-slate-500 mt-1">{kol?.description || "历史信号与近 30 天表现"}</p>
        </div>
        {kol?.followed && <Badge tone="accent">已关注</Badge>}
      </div>

      <div className="grid grid-cols-2 lg:grid-cols-4 gap-3 md:gap-4">
        <Stat label="近 30 天盈亏" value={fmtMoney(rank.total_pnl ?? kol?.cached_pnl, 2)} tone={(rank.total_pnl ?? kol?.cached_pnl) >= 0 ? "profit" : "loss"} sub="USDT" />
        <Stat label="胜率" value={`${rank.win_rate ?? 0}%`} sub={`${rank.trade_count ?? 0} 笔成交`} />
        <Stat label="信号数" value={rank.signal_count ?? kol?.cached_signal_count ?? 0} sub="近 30 天" />
        <Stat label="成交数" value={rank.trade_count ?? 0} sub="跟单成交" />
      </div>

      <Card>
        <CardTitle action={<Trophy size={16} className="text-warn" />}>收益概览</CardTitle>
        <div className="h-52 flex items-center justify-center text-sm text-slate-500 border border-dashed border-border rounded-xl">
          当前后端暂未提供单个 KOL 收益曲线接口，已展示可用的排行收益与历史信号。
        </div>
      </Card>

      <Card>
        <CardTitle action={<Radio size={16} className="text-accent-glow" />}>历史信号</CardTitle>
        {signals.length === 0 ? (
          <Empty text="暂无历史信号" />
        ) : (
          <div className="space-y-2.5">
            {signals.map((sig) => {
              const p = sig.parsed || {};
              return (
                <div key={sig.id} className="glass-soft p-3.5">
                  <div className="flex items-center gap-2 flex-wrap">
                    {sig.symbol && <Badge tone="accent">{sig.symbol}</Badge>}
                    {p.side && <Badge tone={p.side === "long" ? "profit" : "loss"}>{p.side}</Badge>}
                    <Badge>{sig.status}</Badge>
                    {sig.confidence > 0 && <span className="text-xs text-slate-500"><Percent size={10} className="inline" /> {(sig.confidence * 100).toFixed(0)}%</span>}
                    <span className="text-xs text-slate-600 ml-auto">{fmtTime(sig.received_at)}</span>
                  </div>
                  <div className="text-xs md:text-sm text-slate-400 mt-2 font-mono whitespace-pre-wrap break-all line-clamp-3">
                    {sig.raw_text || "(无文本)"}
                  </div>
                  {(p.take_profits?.length || p.stop_loss) && (
                    <div className="flex gap-3 mt-2 text-xs flex-wrap">
                      {p.take_profits?.length > 0 && <span className="text-profit">TP: {p.take_profits.map((t: number) => fmtMoney(t, 4)).join(" / ")}</span>}
                      {p.stop_loss && <span className="text-loss">SL: {fmtMoney(p.stop_loss, 4)}</span>}
                    </div>
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
