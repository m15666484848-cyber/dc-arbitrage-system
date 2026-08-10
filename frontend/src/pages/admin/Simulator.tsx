import { useMemo, useState } from "react";
import { FlaskConical, Play, ShieldCheck, AlertTriangle, CheckCircle2, XCircle, Copy } from "lucide-react";
import { API } from "@/api/client";
import { Card, CardTitle, Button, Input, Select, Badge, Field } from "@/components/ui";
import { useToast } from "@/components/ui/Toast";
import { fmtMoney } from "@/lib/utils";

function TextArea({ className = "", ...props }: React.TextareaHTMLAttributes<HTMLTextAreaElement>) {
  return <textarea className={`input min-h-[150px] resize-y ${className}`} {...props} />;
}

function StepBadge({ status }: { status: string }) {
  if (status === "ok") return <Badge tone="profit"><CheckCircle2 size={11} /> 通过</Badge>;
  if (status === "corrected") return <Badge tone="warn"><AlertTriangle size={11} /> 已修正</Badge>;
  if (status === "reject") return <Badge tone="loss"><XCircle size={11} /> 拒绝</Badge>;
  if (status === "ignored") return <Badge tone="default">忽略</Badge>;
  return <Badge tone="warn">{status}</Badge>;
}

function JsonBlock({ data }: { data: any }) {
  const text = useMemo(() => JSON.stringify(data, null, 2), [data]);
  const { push } = useToast();
  return (
    <div className="relative">
      <Button
        variant="ghost"
        className="absolute right-2 top-2 px-2 py-1 text-xs"
        onClick={() => {
          navigator.clipboard?.writeText(text);
          push("success", "已复制JSON");
        }}
      >
        <Copy size={12} /> 复制
      </Button>
      <pre className="text-xs leading-5 overflow-auto max-h-[460px] rounded-xl bg-black/30 border border-white/10 p-4 pr-20 text-slate-300">{text}</pre>
    </div>
  );
}

export default function AdminSimulator() {
  const { push } = useToast();
  const [message, setMessage] = useState("比特币做多 现价：65000 止损：63800 止盈：待定");
  const [marketPrice, setMarketPrice] = useState("");
  const [tpLevels, setTpLevels] = useState("3,5,8");
  const [defaultSlPct, setDefaultSlPct] = useState("-0.05");
  const [maxSlPct, setMaxSlPct] = useState("");
  const [noStopLoss, setNoStopLoss] = useState(false);
  const [loading, setLoading] = useState(false);
  const [result, setResult] = useState<any>(null);

  const run = async () => {
    const text = message.trim();
    if (!text) {
      push("error", "请输入KOL消息");
      return;
    }
    setLoading(true);
    setResult(null);
    try {
      const levels = tpLevels
        .split(",")
        .map((v) => Number(v.trim()))
        .filter((v) => Number.isFinite(v) && v > 0);
      const res = await API.simulateKolSignal({
        message: text,
        market_price: marketPrice ? Number(marketPrice) : null,
        tp_levels: levels.length ? levels : [3, 5, 8],
        default_sl_pct: defaultSlPct ? Number(defaultSlPct) : -0.05,
        no_stop_loss: noStopLoss,
        max_sl_pct: maxSlPct ? Number(maxSlPct) : null,
      });
      setResult(res);
      push(res.decision === "reject" ? "error" : "success", res.decision === "reject" ? "模拟结果：会被拒绝" : "模拟完成");
    } catch (e: any) {
      push("error", e?.response?.data?.detail || e?.response?.data?.message || "模拟失败");
    } finally {
      setLoading(false);
    }
  };

  const parsed = result?.parsed_after || {};
  const order = result?.order_preview;

  return (
    <div className="space-y-4">
      <div className="flex flex-col sm:flex-row sm:items-center justify-between gap-3">
        <div>
          <h1 className="text-xl font-bold gradient-text flex items-center gap-2 font-display">
            <FlaskConical size={20} /> KOL信号模拟测试
          </h1>
          <p className="text-sm text-text-tertiary mt-1">输入一条KOL消息，查看系统会如何解析、纠错、补止盈止损和预览下单。不会真实下单。</p>
        </div>
        <Badge tone="profit"><ShieldCheck size={12} /> 安全模拟，不写订单</Badge>
      </div>

      <div className="grid grid-cols-1 xl:grid-cols-3 gap-4">
        <Card className="xl:col-span-1">
          <CardTitle>输入参数</CardTitle>
          <div className="space-y-3">
            <Field label="KOL消息文本">
              <TextArea value={message} onChange={(e) => setMessage(e.target.value)} placeholder="例如：BTC做多 现价65000 止损63800 止盈待定" />
            </Field>
            <div className="grid grid-cols-2 gap-3">
              <Field label="当前市场价(可选)">
                <Input value={marketPrice} onChange={(e) => setMarketPrice(e.target.value)} placeholder="不填则用入场价" />
              </Field>
              <Field label="默认止盈%">
                <Input value={tpLevels} onChange={(e) => setTpLevels(e.target.value)} placeholder="3,5,8" />
              </Field>
              <Field label="默认止损比例">
                <Input value={defaultSlPct} onChange={(e) => setDefaultSlPct(e.target.value)} placeholder="-0.05" />
              </Field>
              <Field label="最大亏损比例(可选)">
                <Input value={maxSlPct} onChange={(e) => setMaxSlPct(e.target.value)} placeholder="如 0.05" />
              </Field>
            </div>
            <Field label="止损模式">
              <Select value={noStopLoss ? "true" : "false"} onChange={(e) => setNoStopLoss(e.target.value === "true")}>
                <option value="false">正常止损模式</option>
                <option value="true">无止损模式(仅模拟)</option>
              </Select>
            </Field>
            <Button onClick={run} disabled={loading} className="w-full justify-center">
              <Play size={15} /> {loading ? "模拟中..." : "开始模拟"}
            </Button>
          </div>
        </Card>

        <div className="xl:col-span-2 space-y-4">
          {result ? (
            <>
              <Card>
                <CardTitle action={
                  result.decision === "reject" ? <Badge tone="loss">会拒绝</Badge> : <Badge tone="profit">会通过</Badge>
                }>
                  处理摘要
                </CardTitle>
                <div className="grid grid-cols-2 md:grid-cols-4 gap-3">
                  <div className="glass-soft p-3">
                    <div className="text-xs text-slate-500">品种</div>
                    <div className="font-mono text-slate-100 mt-1">{parsed.symbol || "未识别"}</div>
                  </div>
                  <div className="glass-soft p-3">
                    <div className="text-xs text-slate-500">方向</div>
                    <div className="font-mono text-slate-100 mt-1">{parsed.side || "未识别"}</div>
                  </div>
                  <div className="glass-soft p-3">
                    <div className="text-xs text-slate-500">入场</div>
                    <div className="font-mono text-slate-100 mt-1">{parsed.entry_price ? fmtMoney(parsed.entry_price, 4) : "未识别"}</div>
                  </div>
                  <div className="glass-soft p-3">
                    <div className="text-xs text-slate-500">止损</div>
                    <div className="font-mono text-slate-100 mt-1">{parsed.stop_loss ? fmtMoney(parsed.stop_loss, 4) : "未识别"}</div>
                  </div>
                </div>
                {result.reject_reason && (
                  <div className="mt-3 p-3 rounded-xl border border-loss/30 bg-loss/10 text-loss text-sm">
                    拒绝原因：{result.reject_reason}
                  </div>
                )}
              </Card>

              <Card>
                <CardTitle>分析依据</CardTitle>
                <div className="space-y-2">
                  {(result.analysis_basis || []).map((b: any, idx: number) => (
                    <div key={idx} className="glass-soft p-3">
                      <div className="flex flex-col sm:flex-row sm:items-center sm:justify-between gap-2">
                        <div className="text-sm font-medium text-slate-100">{b.item}</div>
                        <Badge tone="accent">依据 {idx + 1}</Badge>
                      </div>
                      <div className="mt-2 grid grid-cols-1 lg:grid-cols-3 gap-2 text-xs">
                        <div>
                          <div className="text-slate-500 mb-1">规则</div>
                          <div className="text-slate-300">{b.rule}</div>
                        </div>
                        <div>
                          <div className="text-slate-500 mb-1">命中文本</div>
                          <div className="font-mono text-slate-300 break-all">{b.evidence}</div>
                        </div>
                        <div>
                          <div className="text-slate-500 mb-1">判断结果</div>
                          <div className="text-slate-100">{b.result}</div>
                        </div>
                      </div>
                    </div>
                  ))}
                </div>
              </Card>

              <Card>
                <CardTitle>处理步骤</CardTitle>
                <div className="space-y-2">
                  {(result.steps || []).map((s: any, idx: number) => (
                    <div key={idx} className="glass-soft p-3 flex flex-col sm:flex-row sm:items-center gap-2 justify-between">
                      <div>
                        <div className="text-sm font-medium text-slate-100">{s.name}</div>
                        <div className="text-xs text-slate-500 mt-1">{s.message}</div>
                      </div>
                      <StepBadge status={s.status} />
                    </div>
                  ))}
                </div>
              </Card>

              {order && (
                <Card>
                  <CardTitle>预计下单与止盈分级</CardTitle>
                  <div className="grid grid-cols-1 md:grid-cols-2 gap-3 mb-3">
                    <div className="glass-soft p-3 text-sm text-slate-300">开仓方向：<span className="font-mono text-slate-100">{order.order_side}</span></div>
                    <div className="glass-soft p-3 text-sm text-slate-300">参考入场：<span className="font-mono text-slate-100">{fmtMoney(order.entry_price, 4)}</span></div>
                  </div>
                  <div className="overflow-auto">
                    <table className="w-full text-sm">
                      <thead className="text-slate-500">
                        <tr className="border-b border-white/10">
                          <th className="py-2 text-left">级别</th>
                          <th className="py-2 text-right">价格</th>
                          <th className="py-2 text-right">平仓比例</th>
                          <th className="py-2 text-center">状态</th>
                        </tr>
                      </thead>
                      <tbody>
                        {(order.tp_levels || []).map((tp: any) => (
                          <tr key={tp.level} className="border-b border-white/5">
                            <td className="py-2">TP{tp.level}</td>
                            <td className="py-2 text-right font-mono">{fmtMoney(tp.price, 4)}</td>
                            <td className="py-2 text-right font-mono">{((tp.pct || 0) * 100).toFixed(2)}%</td>
                            <td className="py-2 text-center"><Badge tone="default">{tp.status}</Badge></td>
                          </tr>
                        ))}
                      </tbody>
                    </table>
                  </div>
                </Card>
              )}

              <Card>
                <CardTitle>完整返回数据</CardTitle>
                <JsonBlock data={result} />
              </Card>
            </>
          ) : (
            <Card className="min-h-[320px] flex items-center justify-center">
              <div className="text-center text-slate-500">
                <FlaskConical size={36} className="mx-auto mb-3 opacity-60" />
                输入KOL消息后点击开始模拟，这里会显示系统处理结果。
              </div>
            </Card>
          )}
        </div>
      </div>
    </div>
  );
}
