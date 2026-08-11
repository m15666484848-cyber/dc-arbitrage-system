import { useEffect, useMemo, useState } from "react";
import { FlaskConical, Play, ShieldCheck, AlertTriangle, CheckCircle2, XCircle, Copy, ListChecks, Save, Trash2, RefreshCw } from "lucide-react";
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

function SingleMessagePanel() {
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

const DEFAULT_EXPECTED = `{
  "symbol": "BTC/USDT",
  "side": "long",
  "entry_price": 65000,
  "stop_loss": 63800
}`;

function RegressionPanel() {
  const { push } = useToast();
  const [cases, setCases] = useState<any[]>([]);
  const [loading, setLoading] = useState(false);
  const [running, setRunning] = useState(false);
  const [editingId, setEditingId] = useState<number | null>(null);
  const [name, setName] = useState("");
  const [rawText, setRawText] = useState("比特币做多 现价：65000 止损：63800 止盈：待定");
  const [tags, setTags] = useState("BTC,开多");
  const [enabled, setEnabled] = useState(true);
  const [note, setNote] = useState("");
  const [expectedText, setExpectedText] = useState(DEFAULT_EXPECTED);
  const [runResult, setRunResult] = useState<any>(null);
  const [bulkText, setBulkText] = useState("");
  const [bulkEnabled, setBulkEnabled] = useState(false);
  const [bulkImporting, setBulkImporting] = useState(false);
  const [bulkResult, setBulkResult] = useState<any>(null);
  const [importReport, setImportReport] = useState<any>(null);
  const [importReports, setImportReports] = useState<any[]>([]);
  const [selectedImportReport, setSelectedImportReport] = useState<any>(null);
  const [refreshingReport, setRefreshingReport] = useState<string | null>(null);
  const [casePage, setCasePage] = useState(1);
  const [casePageSize] = useState(100);
  const [caseTotal, setCaseTotal] = useState(0);
  const [casePages, setCasePages] = useState(1);
  const [caseQ, setCaseQ] = useState("");
  const [caseEnabledFilter, setCaseEnabledFilter] = useState("all");

  const loadCases = async (pageArg = casePage) => {
    setLoading(true);
    try {
      const res = await API.listParserRegressionCases({
        page: pageArg,
        page_size: casePageSize,
        q: caseQ.trim() || undefined,
        enabled: caseEnabledFilter === "all" ? undefined : caseEnabledFilter === "enabled",
      });
      const rows = Array.isArray(res) ? res : (res.items || []);
      setCases(rows);
      setCaseTotal(Array.isArray(res) ? rows.length : (res.total || 0));
      setCasePages(Array.isArray(res) ? 1 : (res.pages || 1));
      setCasePage(Array.isArray(res) ? 1 : (res.page || pageArg));
    } catch (e: any) {
      push("error", e?.response?.data?.detail || e?.response?.data?.message || "加载回归用例失败");
    } finally {
      setLoading(false);
    }
  };

  const loadImportReports = async () => {
    try {
      const rows = await API.listParserRegressionImportReports();
      setImportReports(rows || []);
    } catch (e: any) {
      push("error", e?.response?.data?.detail || e?.response?.data?.message || "加载导入报告失败");
    }
  };

  useEffect(() => {
    loadCases(casePage);
  }, [casePage, caseQ, caseEnabledFilter]);

  useEffect(() => {
    loadImportReports();
  }, []);

  // 动作类型中文映射
  const ACTION_LABELS: Record<string, string> = {
    ignored: "忽略/噪音",
    open_long: "开多",
    open_short: "开空",
    close_position: "平仓",
    cancel_order: "撤单",
    update_tp_sl: "更新止盈止损",
    hold_pending: "持仓待定",
    refresh_pending: "刷新待定",
  };
  const formatActionCounts = (counts: Record<string, number> = {}) => {
    return Object.entries(counts)
      .sort((a, b) => Number(b[1]) - Number(a[1]))
      .map(([k, v]) => `${ACTION_LABELS[k] || k}: ${v}`)
      .join(" / ");
  };
  // 健康状态等级
  const getHealthLevel = (summary: any = {}) => {
    const rate = Number(summary.execution_success_rate || 0);
    const high = Number(summary.high_risk || 0);
    const risk = Number(summary.risk_count || 0);
    if (rate >= 85 && high === 0) return { label: "优秀", tone: "profit", color: "text-profit", bg: "bg-profit/10 border-profit/30", desc: "解析质量优秀，可直接启用回归" };
    if (rate >= 70 && high <= 2) return { label: "良好", tone: "accent", color: "text-accent", bg: "bg-accent/10 border-accent/30", desc: "解析质量良好，少量样本需复查" };
    if (rate >= 40 || high <= 5) return { label: "警告", tone: "warn", color: "text-warn", bg: "bg-warn/10 border-warn/30", desc: "存在较多风险样本，需修复后再启用" };
    return { label: "危险", tone: "loss", color: "text-loss", bg: "bg-loss/10 border-loss/30", desc: "高风险样本过多，必须优先修复" };
  };

  const mergeImportReports = (base: any, next: any) => {
    if (!next) return base;
    if (!base) return next;
    const summaryKeys = ["total_messages", "diagnosed", "created_cases", "success_count", "failed_count", "effective_executable_count", "not_executed_count", "execution_success_rate", "normal_execution_rate", "import_success_rate", "strategy_count", "noise_count", "skipped_noise", "risk_count", "high_risk", "medium_risk", "low_risk", "normal_execution_count", "warning_count"];
    const summary: any = { ...(base.summary || {}) };
    for (const key of summaryKeys) summary[key] = (summary[key] || 0) + ((next.summary || {})[key] || 0);
    const mergeCounts = (a: any = {}, b: any = {}) => {
      const out: any = { ...a };
      Object.entries(b).forEach(([k, v]) => { out[k] = (out[k] || 0) + Number(v || 0); });
      return out;
    };
    const top_issues = [...(base.top_issues || []), ...(next.top_issues || [])]
      .sort((a, b) => (b.score || 0) - (a.score || 0))
      .slice(0, 120);
    const category_counts = mergeCounts(base.category_counts, next.category_counts);
    const groups = Object.entries(category_counts)
      .sort((a: any, b: any) => Number(b[1]) - Number(a[1]))
      .map(([category, count]) => ({
        category,
        count,
        samples: top_issues.filter((i: any) => i.category === category).slice(0, 5),
      }));
    const fix_suggestions = Array.from(new Set([...(base.fix_suggestions || []), ...(next.fix_suggestions || [])]));
    return {
      summary,
      overall_analysis: next.overall_analysis || base.overall_analysis,
      fix_suggestions,
      risk_counts: mergeCounts(base.risk_counts, next.risk_counts),
      action_counts: mergeCounts(base.action_counts, next.action_counts),
      category_counts,
      groups,
      top_issues,
    };
  };

  const resetForm = () => {
    setEditingId(null);
    setName("");
    setRawText("比特币做多 现价：65000 止损：63800 止盈：待定");
    setTags("BTC,开多");
    setEnabled(true);
    setNote("");
    setExpectedText(DEFAULT_EXPECTED);
  };

  const parseExpected = () => {
    try {
      const parsed = JSON.parse(expectedText || "{}");
      if (!parsed || typeof parsed !== "object" || Array.isArray(parsed)) {
        throw new Error("期望结果必须是 JSON 对象");
      }
      return parsed;
    } catch (e: any) {
      push("error", `期望 JSON 格式错误: ${e.message || e}`);
      return null;
    }
  };

  const saveCase = async () => {
    const expected = parseExpected();
    if (!expected) return;
    if (!rawText.trim()) {
      push("error", "请输入 KOL 消息原文");
      return;
    }
    const payload = {
      name: name.trim() || rawText.trim().slice(0, 40),
      raw_text: rawText,
      expected,
      enabled,
      tags,
      note,
    };
    try {
      if (editingId) {
        await API.updateParserRegressionCase(editingId, payload);
        push("success", "用例已更新");
      } else {
        await API.createParserRegressionCase(payload);
        push("success", "用例已新增");
      }
      resetForm();
      loadCases(casePage);
    } catch (e: any) {
      push("error", e?.response?.data?.detail || e?.response?.data?.message || "保存用例失败");
    }
  };

  const editCase = (item: any) => {
    setEditingId(item.id);
    setName(item.name || "");
    setRawText(item.raw_text || "");
    setTags(item.tags || "");
    setEnabled(item.enabled !== false);
    setNote(item.note || "");
    setExpectedText(JSON.stringify(item.expected || {}, null, 2));
  };

  const deleteCase = async (id: number) => {
    if (!window.confirm("确定删除这个回归用例吗？")) return;
    try {
      await API.deleteParserRegressionCase(id);
      push("success", "用例已删除");
      if (editingId === id) resetForm();
      loadCases(casePage);
    } catch (e: any) {
      push("error", e?.response?.data?.detail || e?.response?.data?.message || "删除失败");
    }
  };

  const viewImportReport = (item: any) => {
    setSelectedImportReport(item);
    setImportReport(item.report || {});
  };

  const filterImportCases = (item: any, highOnly = false) => {
    const batch = item.import_batch_id;
    setCaseQ(highOnly ? `批次:${batch} 高风险待确认` : `批次:${batch}`);
    setCaseEnabledFilter("all");
    setCasePage(1);
  };

  const refreshImportReport = async (item: any) => {
    const batch = item.import_batch_id;
    setRefreshingReport(batch);
    try {
      const refreshed = await API.refreshParserRegressionImportReport(batch);
      setImportReports((rows) => rows.map((row) => row.import_batch_id === batch ? refreshed : row));
      setSelectedImportReport(refreshed);
      setImportReport(refreshed.report || {});
      push("success", "导入报告已刷新，并已生成与上次报告的对比");
    } catch (e: any) {
      push("error", e?.response?.data?.detail || e?.response?.data?.message || "刷新导入报告失败");
    } finally {
      setRefreshingReport(null);
    }
  };

  const deleteImportBatch = async (item: any) => {
    const batch = item.import_batch_id;
    if (!window.confirm(`确定删除本次导入吗？\n文件：${item.source_file}\n批次：${batch}\n会删除该批次生成的用例和报告。`)) return;
    try {
      const res = await API.bulkDeleteParserRegressionCases({ mode: "filtered", q: `批次:${batch}` });
      await API.deleteParserRegressionImportReport(batch);
      push("success", `已删除本次导入用例 ${res.deleted || 0} 条，并删除报告`);
      if (selectedImportReport?.import_batch_id === batch) {
        setSelectedImportReport(null);
        setImportReport(null);
      }
      loadCases(1);
      loadImportReports();
    } catch (e: any) {
      push("error", e?.response?.data?.detail || e?.response?.data?.message || "删除本次导入失败");
    }
  };

  const bulkDeleteCases = async (mode: "filtered" | "drafts" | "all") => {
    try {
      let payload: any = { mode };
      if (mode === "filtered") {
        const q = caseQ.trim();
        const enabledFilter = caseEnabledFilter === "all" ? undefined : caseEnabledFilter === "enabled";
        if (!q && enabledFilter === undefined) {
          push("error", "请先输入搜索/标签，或选择启用/草稿状态后再删除当前筛选结果");
          return;
        }
        const label = [
          q ? `关键词“${q}”` : "",
          enabledFilter === true ? "只看启用" : enabledFilter === false ? "只看草稿停用" : "",
        ].filter(Boolean).join(" + ");
        if (!window.confirm(`确定删除当前筛选结果吗？\n筛选条件：${label}\n该操作不可恢复。`)) return;
        payload = { mode, q: q || undefined, enabled: enabledFilter };
      } else if (mode === "drafts") {
        if (!window.confirm("确定删除全部草稿停用用例吗？\n已启用的黄金用例不会删除。")) return;
      } else {
        const confirmText = window.prompt("危险操作：将删除全部回归用例。\n如确认，请输入 DELETE ALL");
        if (confirmText !== "DELETE ALL") {
          push("error", "确认文字不匹配，已取消删除全部用例");
          return;
        }
        payload = { mode, confirm: confirmText };
      }
      const res = await API.bulkDeleteParserRegressionCases(payload);
      push("success", `已删除 ${res.deleted || 0} 条回归用例`);
      if (editingId) resetForm();
      setCasePage(1);
      loadCases(1);
    } catch (e: any) {
      push("error", e?.response?.data?.detail || e?.response?.data?.message || "批量删除失败");
    }
  };

  const runCases = async (ids?: number[]) => {
    setRunning(true);
    try {
      const res = await API.runParserRegression({ ids, enabled_only: !ids });
      setRunResult(res);
      push(res.failed ? "error" : "success", `回归完成: 通过 ${res.passed}/${res.total}`);
    } catch (e: any) {
      push("error", e?.response?.data?.detail || e?.response?.data?.message || "运行回归测试失败");
    } finally {
      setRunning(false);
    }
  };

  const bulkImportCases = async () => {
    const text = bulkText.trim();
    if (!text) {
      push("error", "请先粘贴历史 KOL 消息");
      return;
    }
    setBulkImporting(true);
    try {
      const res = await API.bulkImportParserRegressionCases({
        raw_text: text,
        enabled: bulkEnabled,
        tag_prefix: "批量导入",
        include_noise: true,
        max_cases: 200,
      });
      setBulkResult(res);
      setImportReport(res.report || null);
      setBulkText("");
      push("success", `已导入 ${res.created || 0} 条草稿用例，已生成解析诊断报告`);
      loadCases(casePage);
    } catch (e: any) {
      push("error", e?.response?.data?.detail || e?.response?.data?.message || "批量导入失败");
    } finally {
      setBulkImporting(false);
    }
  };

  const splitBulkTextForImport = (raw: string) => {
    const text = (raw || "").replace(/\r\n/g, "\n").replace(/\r/g, "\n").trim();
    if (!text) return [] as string[];
    let chunks = text.split(/\n\s*\n+|\n\s*[-=]{3,}\s*\n/).map((c) => c.trim()).filter(Boolean);
    if (chunks.length <= 1) {
      chunks = text.split("\n").map((c) => c.trim()).filter(Boolean);
    }
    const out: string[] = [];
    const seen = new Set<string>();
    for (const rawChunk of chunks) {
      const chunk = rawChunk
        .replace(/^\s*\[?\d{4}[-/]\d{1,2}[-/]\d{1,2}[^\n]{0,40}\]?\s*/, "")
        .replace(/^\s*(?:KOL|作者|Author|From)\s*[:：].*?\n/i, "")
        .trim();
      if (!chunk || seen.has(chunk)) continue;
      seen.add(chunk);
      out.push(chunk);
    }
    return out;
  };

  const importBulkFile = async (e: React.ChangeEvent<HTMLInputElement>) => {
    const file = e.target.files?.[0];
    e.target.value = "";
    if (!file) return;
    const allowedExt = [".txt", ".csv", ".json", ".md", ".log"];
    const lowerName = file.name.toLowerCase();
    if (!allowedExt.some((ext) => lowerName.endsWith(ext))) {
      push("error", "暂只支持 .txt / .csv / .json / .md / .log 文本文件");
      return;
    }
    if (file.size > 50 * 1024 * 1024) {
      push("error", "文件太大，单个文件不能超过 50MB");
      return;
    }
    setBulkImporting(true);
    try {
      const text = await file.text();
      const messages = splitBulkTextForImport(text);
      if (!messages.length) {
        push("error", "文件内容为空，或没有识别到可导入的消息");
        return;
      }

      const batchSize = 800;
      const totalBatches = Math.ceil(messages.length / batchSize);
      const importBatchId = `${new Date().toISOString().replace(/[-:.TZ]/g, "").slice(0, 14)}_${Math.random().toString(36).slice(2, 8)}`;
      let totalCreated = 0;
      let totalSkippedNoise = 0;
      let mergedReport: any = null;
      setImportReport(null);
      setSelectedImportReport(null);
      push("success", `已识别 ${messages.length} 条消息，将自动分 ${totalBatches} 批导入并生成诊断报告`);

      for (let start = 0; start < messages.length; start += batchSize) {
        const batchNo = Math.floor(start / batchSize) + 1;
        const batch = messages.slice(start, start + batchSize);
        const res = await API.bulkImportParserRegressionCases({
          raw_text: batch.join("\n\n---\n\n"),
          enabled: bulkEnabled,
          tag_prefix: `文件导入:${file.name.slice(0, 32)},批次:${importBatchId},#${batchNo}/${totalBatches}`,
          include_noise: true,
          max_cases: 1000,
        });
        totalCreated += res.created || 0;
        totalSkippedNoise += res.skipped_noise || 0;
        mergedReport = mergeImportReports(mergedReport, res.report);
        setBulkResult({ ...res, created: totalCreated, skipped_noise: totalSkippedNoise, enabled: bulkEnabled });
        setImportReport(mergedReport);
      }

      const finalReport = {
        ...(mergedReport || {}),
        import_batch_id: importBatchId,
        source_file: file.name,
        generated_at: new Date().toISOString(),
      };
      setImportReport(finalReport);
      setSelectedImportReport({ import_batch_id: importBatchId, source_file: file.name, report: finalReport });
      await API.saveParserRegressionImportReport({
        import_batch_id: importBatchId,
        source_file: file.name,
        total_messages: messages.length,
        created_cases: totalCreated,
        report: finalReport,
      });
      push("success", `文件导入完成：${file.name}，共生成 ${totalCreated} 条草稿用例，诊断报告已保存`);
      loadCases(casePage);
      loadImportReports();
    } catch (err: any) {
      push("error", err?.response?.data?.detail || err?.response?.data?.message || "文件导入失败，请确认文件是 UTF-8 文本格式");
    } finally {
      setBulkImporting(false);
    }
  };

  const results: any[] = runResult?.results || [];

  return (
    <div className="space-y-4">
      <div className="flex flex-col sm:flex-row sm:items-center justify-between gap-3">
        <div>
          <h2 className="text-lg font-bold text-text flex items-center gap-2">
            <ListChecks size={18} /> 解析回归测试
          </h2>
          <p className="text-sm text-text-tertiary mt-1">保存典型 KOL 消息和期望解析结果，一键检查改 parser 后有没有退化。不会真实下单。</p>
        </div>
        <div className="flex gap-2 flex-wrap">
          <Button variant="ghost" onClick={() => loadCases(casePage)} disabled={loading}>
            <RefreshCw size={14} /> 刷新
          </Button>
          <Button onClick={() => runCases()} disabled={running}>
            <Play size={14} /> {running ? "运行中..." : "运行全部启用用例"}
          </Button>
        </div>
      </div>

      <Card>
        <CardTitle action={<Badge tone={bulkEnabled ? "warn" : "default"}>{bulkEnabled ? "导入后启用" : "导入为草稿"}</Badge>}>
          批量导入历史消息
        </CardTitle>
        <div className="space-y-3">
          <p className="text-xs text-text-tertiary">
            可以一次性粘贴大量 KOL 历史消息，支持策略消息和无用聊天混在一起。系统会自动拆分、解析并生成“待确认”用例；建议先保持停用，人工确认期望 JSON 后再启用。
          </p>
          <Field label="批量消息文本">
            <TextArea
              className="min-h-[180px]"
              value={bulkText}
              onChange={(e) => setBulkText(e.target.value)}
              placeholder={"每条消息一行，或用空行分隔多段消息。\n例如：\nBTC 做多 现价 65000 止损 63800\n\n今天行情波动大，先观察。"}
            />
          </Field>
            <Field label="或直接选择历史消息文件">
              <input
                type="file"
                accept=".txt,.csv,.json,.md,.log,text/plain,text/csv,application/json,text/markdown"
                disabled={bulkImporting}
                onChange={importBulkFile}
                className="input cursor-pointer"
              />
              <div className="mt-1 text-xs text-text-muted">支持 .txt / .csv / .json / .md / .log，单个文件不能超过 50MB；大文件会自动分批导入。</div>
            </Field>
          <div className="flex flex-col sm:flex-row sm:items-center gap-3">
            <Field label="导入状态">
              <Select value={bulkEnabled ? "true" : "false"} onChange={(e) => setBulkEnabled(e.target.value === "true")}>
                <option value="false">导入为草稿（推荐）</option>
                <option value="true">导入后直接启用</option>
              </Select>
            </Field>
            <Button onClick={bulkImportCases} disabled={bulkImporting || !bulkText.trim()} className="sm:mt-5">
              <Save size={14} /> {bulkImporting ? "导入中..." : "批量导入"}
            </Button>
          </div>
          {bulkResult && (
            <div className="text-xs text-text-tertiary glass-soft p-3">
              最近导入：{bulkResult.created || 0} 条，用例默认{bulkResult.enabled ? "已启用" : "为草稿停用"}。请在下方列表检查 expected JSON。
            </div>
          )}
          {importReport && (
            <div className="glass-soft p-4 space-y-4">
              <div className="flex flex-col lg:flex-row lg:items-center justify-between gap-3">
                <div>
                  <div className="font-semibold text-text">解析诊断报告</div>
                  <div className="text-xs text-text-muted mt-1">按风险排序，只需要优先看 P0/P1 样本。</div>
                </div>
                <div className="flex gap-2 flex-wrap">
                  <Badge tone="loss">P0 高风险 {importReport.summary?.high_risk || 0}</Badge>
                  <Badge tone="warn">P1 中风险 {importReport.summary?.medium_risk || 0}</Badge>
                  <Badge tone="default">P2 低风险 {importReport.summary?.low_risk || 0}</Badge>
                  <Badge tone="profit">有效可执行 {importReport.summary?.effective_executable_count || 0}</Badge>
                  <Badge tone="profit">正常执行 {importReport.summary?.normal_execution_count || 0}</Badge>
                </div>
              </div>
              {(() => {
                const h = getHealthLevel(importReport.summary);
                return (
                  <div className={`rounded-xl border p-4 flex items-center gap-4 ${h.bg}`}>
                    <div className={`text-3xl font-bold ${h.color}`}>{h.label}</div>
                    <div className="flex-1">
                      <div className={`text-sm font-semibold ${h.color}`}>健康状态：{h.label}</div>
                      <div className="text-xs text-text-secondary mt-1">{h.desc}</div>
                    </div>
                    <div className="text-right">
                      <div className="text-2xl font-mono text-profit">{importReport.summary?.execution_success_rate || 0}%</div>
                      <div className="text-xs text-text-muted">成功率</div>
                    </div>
                  </div>
                );
              })()}
              {/* 第一层：总消息数 = 有效信息 + 噪音/聊天 */}
              <div className="glass-soft p-3">
                <div className="text-xs text-text-muted mb-2">第一层：消息总数拆分</div>
                <div className="flex items-center gap-3 flex-wrap text-sm">
                  <span className="text-text-muted">总消息</span>
                  <span className="text-lg font-mono font-bold text-text">{importReport.summary?.total_messages || 0}</span>
                  <span className="text-text-muted">=</span>
                  <span className="text-accent font-mono">有效信息 {importReport.summary?.strategy_count || 0}</span>
                  <span className="text-text-muted">+</span>
                  <span className="text-text-tertiary font-mono">噪音/聊天 {importReport.summary?.noise_count || 0}</span>
                </div>
              </div>
              {/* 第二层：有效信息 = 有效可执行 + 未执行 */}
              <div className="glass-soft p-3">
                <div className="text-xs text-text-muted mb-2">第二层：有效信息拆分（只看策略消息，噪音不算）</div>
                <div className="flex items-center gap-3 flex-wrap text-sm">
                  <span className="text-text-muted">有效信息</span>
                  <span className="text-lg font-mono font-bold text-accent">{importReport.summary?.strategy_count || 0}</span>
                  <span className="text-text-muted">=</span>
                  <span className="text-profit font-mono">有效可执行 {importReport.summary?.effective_executable_count || 0}</span>
                  <span className="text-text-muted">+</span>
                  <span className="text-text-tertiary font-mono">未执行 {importReport.summary?.not_executed_count || 0}</span>
                </div>
                <div className="text-xs text-text-muted mt-2">
                  其中"正常执行"（无任何风险、可直接执行）<span className="text-profit font-mono">{importReport.summary?.normal_execution_count || 0}</span> 条，"告警样本"（有低风险提醒）<span className="text-warn font-mono">{importReport.summary?.warning_count || 0}</span> 条
                </div>
              </div>
              {/* 第三层：成功率公式 */}
              <div className="glass-soft p-3 border border-profit/20">
                <div className="text-xs text-text-muted mb-2">第三层：成功率计算</div>
                <div className="flex items-center gap-3 flex-wrap text-sm">
                  <span className="text-profit font-mono font-bold text-lg">{importReport.summary?.execution_success_rate || 0}%</span>
                  <span className="text-text-muted">=</span>
                  <span className="text-profit font-mono">有效可执行 {importReport.summary?.effective_executable_count || 0}</span>
                  <span className="text-text-muted">/</span>
                  <span className="text-accent font-mono">有效信息 {importReport.summary?.strategy_count || 0}</span>
                </div>
              </div>
              {/* 风险概览 */}
              <div className="grid grid-cols-2 md:grid-cols-4 gap-3">
                <div className="glass-soft p-3">
                  <div className="text-xs text-text-muted">P0 高风险</div>
                  <div className="text-lg font-mono text-loss mt-1">{importReport.summary?.high_risk || 0}</div>
                </div>
                <div className="glass-soft p-3">
                  <div className="text-xs text-text-muted">P1 中风险</div>
                  <div className="text-lg font-mono text-warn mt-1">{importReport.summary?.medium_risk || 0}</div>
                </div>
                <div className="glass-soft p-3">
                  <div className="text-xs text-text-muted">风险合计</div>
                  <div className="text-lg font-mono text-loss mt-1">{importReport.summary?.risk_count || ((importReport.summary?.high_risk || 0) + (importReport.summary?.medium_risk || 0))}</div>
                </div>
                <div className="glass-soft p-3">
                  <div className="text-xs text-text-muted">生成用例</div>
                  <div className="text-lg font-mono text-text mt-1">{importReport.summary?.created_cases || importReport.summary?.diagnosed || 0}</div>
                </div>
              </div>
              {/* 动作类型分布 */}
              <div className="glass-soft p-3">
                <div className="text-xs text-text-muted mb-2">动作类型分布</div>
                <div className="flex gap-2 flex-wrap">
                  {Object.entries(importReport.action_counts || {}).sort((a: any, b: any) => Number(b[1]) - Number(a[1])).map(([k, v]: any) => (
                    <Badge key={k} tone={k === "ignored" ? "default" : k === "open_long" || k === "open_short" ? "profit" : k === "close_position" || k === "cancel_order" ? "loss" : "warn"}>
                      {ACTION_LABELS[k] || k} {v}
                    </Badge>
                  )) || "无"}
                </div>
              </div>
              {(importReport.overall_analysis || (importReport.fix_suggestions || []).length) && (
                <div className="glass-soft p-3 space-y-2">
                  <div className="text-sm font-semibold text-text">整体分析</div>
                  {importReport.overall_analysis && <div className="text-sm text-text-secondary">{importReport.overall_analysis}</div>}
                  {!!(importReport.fix_suggestions || []).length && (
                    <div className="space-y-1">
                      <div className="text-xs text-text-muted">修复建议</div>
                      {(importReport.fix_suggestions || []).map((tip: string, idx: number) => (
                        <div key={`${tip}-${idx}`} className="text-xs text-warn">· {tip}</div>
                      ))}
                    </div>
                  )}
                </div>
              )}
              {importReport.comparison && (
                <div className="glass-soft p-3 space-y-3">
                  <div className="text-sm font-semibold text-text">对比上次报告</div>
                  <div className="text-xs text-text-secondary">{importReport.comparison.summary}</div>
                  <div className="grid grid-cols-2 md:grid-cols-4 gap-2">
                    {["effective_executable_count", "normal_execution_count", "not_executed_count", "execution_success_rate", "risk_count", "high_risk", "warning_count", "total_messages"].map((key) => {
                      const metric = importReport.comparison?.metrics?.[key] || {};
                      const delta = Number(metric.delta || 0);
                      const suffix = key.includes("rate") ? "%" : "";
                      return (
                        <div key={key} className="rounded-xl border border-border/50 bg-bg/30 p-2">
                          <div className="text-xs text-text-muted">{metric.label || key}</div>
                          <div className="text-sm font-mono text-text mt-1">{metric.current ?? 0}{suffix}</div>
                          <div className={delta > 0 ? "text-xs text-profit" : delta < 0 ? "text-xs text-loss" : "text-xs text-text-tertiary"}>
                            较上次 {delta > 0 ? "+" : ""}{delta}{suffix}
                          </div>
                        </div>
                      );
                    })}
                  </div>
                </div>
              )}
              {!!(importReport.groups || []).length && (
                <div>
                  <div className="text-sm font-semibold text-text mb-2">问题分组</div>
                  <div className="flex gap-2 flex-wrap">
                    {(importReport.groups || []).map((g: any) => (
                      <Badge key={g.category} tone={g.category?.includes("误下单") || g.category?.includes("平仓") ? "loss" : "warn"}>
                        {g.category} {g.count}
                      </Badge>
                    ))}
                  </div>
                </div>
              )}
              <div>
                <div className="text-sm font-semibold text-text mb-2">优先检查样本</div>
                <div className="space-y-2 max-h-[520px] overflow-auto">
                  {(importReport.top_issues || []).slice(0, 30).map((item: any, idx: number) => (
                    <div key={`${item.case_id || item.index}-${idx}`} className="rounded-xl border border-border/60 bg-bg/40 p-3">
                      <div className="flex items-center gap-2 flex-wrap">
                        <Badge tone={item.priority === "P0" ? "loss" : item.priority === "P1" ? "warn" : "default"}>{item.priority}</Badge>
                        <Badge tone="accent">{item.category}</Badge>
                        <span className="text-sm font-semibold text-text">{item.title}</span>
                        {item.case_id && <span className="text-xs text-text-muted">用例 #{item.case_id}</span>}
                      </div>
                      <div className="text-xs text-text-tertiary mt-2 whitespace-pre-wrap break-words line-clamp-3">{item.raw_text}</div>
                      <div className="text-xs text-text-secondary mt-2">原因：{item.reason}</div>
                      <div className="text-xs text-text-muted mt-1">当前解析：{item.actual?.action || "ignored"} {item.actual?.symbol || ""} {item.actual?.side || ""} {item.actual?.position_pct ? `比例${item.actual.position_pct}%` : ""}</div>
                      {item.suggested_action && <div className="text-xs text-warn mt-1">建议检查：{item.suggested_action}</div>}
                    </div>
                  ))}
                </div>
              </div>
            </div>
          )}
        </div>
      </Card>

      <Card>
        <CardTitle action={<Badge tone="default">{importReports.length} 份</Badge>}>导入报告</CardTitle>
        {importReports.length ? (
          <div className="space-y-3">
            <div className="space-y-2 max-h-[360px] overflow-auto">
              {importReports.map((item) => (
                <div key={item.import_batch_id} className="glass-soft p-3">
                  <div className="flex flex-col lg:flex-row lg:items-start justify-between gap-3">
                    <div className="min-w-0">
                      <div className="flex items-center gap-2 flex-wrap">
                        <span className="font-semibold text-text">{item.source_file || "未命名文件"}</span>
                        <Badge tone="loss">P0 {item.high_risk || 0}</Badge>
                        <Badge tone="warn">P1 {item.medium_risk || 0}</Badge>
                        <Badge tone="default">P2 {item.low_risk || 0}</Badge>
                        <Badge tone="profit">有效 {(item.report?.summary?.effective_executable_count || 0)}</Badge>
                        <Badge tone="profit">正常 {(item.report?.summary?.normal_execution_count || 0)}</Badge>
                        <Badge tone="default">成功率 {(item.report?.summary?.execution_success_rate || 0)}%</Badge>
                        {(() => { const h = getHealthLevel(item.report?.summary); return <Badge tone={h.tone as any}>健康：{h.label}</Badge>; })()}
                      </div>
                      <div className="text-xs text-text-muted mt-2">
                        批次：{item.import_batch_id} · 消息 {item.total_messages || 0} · 用例 {item.created_cases || 0}
                      </div>
                    </div>
                    <div className="flex gap-2 flex-wrap shrink-0">
                      <Button variant="ghost" onClick={() => viewImportReport(item)}>查看报告</Button>
                      <Button variant="ghost" onClick={() => refreshImportReport(item)} disabled={refreshingReport === item.import_batch_id}>
                        {refreshingReport === item.import_batch_id ? "刷新中..." : "刷新报告"}
                      </Button>
                      <Button variant="ghost" onClick={() => filterImportCases(item, true)}>只看本批高风险</Button>
                      <Button variant="ghost" onClick={() => filterImportCases(item, false)}>筛选本批用例</Button>
                      <Button variant="danger" onClick={() => deleteImportBatch(item)}>删除本次导入</Button>
                    </div>
                  </div>
                </div>
              ))}
            </div>
            {selectedImportReport && (
              <div className="text-xs text-text-tertiary">
                当前查看：{selectedImportReport.source_file || "未命名文件"} / 批次 {selectedImportReport.import_batch_id}
              </div>
            )}
          </div>
        ) : (
          <div className="text-sm text-text-tertiary py-6 text-center">暂无持久化导入报告。下次文件导入完成后会自动保存。</div>
        )}
      </Card>

      <div className="grid grid-cols-1 xl:grid-cols-3 gap-4">
        <Card className="xl:col-span-1">
          <CardTitle action={editingId ? <Badge tone="accent">编辑 #{editingId}</Badge> : <Badge tone="profit">新增用例</Badge>}>
            用例配置
          </CardTitle>
          <div className="space-y-3">
            <Field label="用例名称">
              <Input value={name} onChange={(e) => setName(e.target.value)} placeholder="例如：BTC 中文做多带止损" />
            </Field>
            <Field label="KOL 消息原文">
              <TextArea value={rawText} onChange={(e) => setRawText(e.target.value)} placeholder="粘贴一条典型 KOL 消息" />
            </Field>
            <div className="grid grid-cols-2 gap-3">
              <Field label="标签">
                <Input value={tags} onChange={(e) => setTags(e.target.value)} placeholder="BTC,开多,中文" />
              </Field>
              <Field label="状态">
                <Select value={enabled ? "true" : "false"} onChange={(e) => setEnabled(e.target.value === "true")}>
                  <option value="true">启用</option>
                  <option value="false">停用</option>
                </Select>
              </Field>
            </div>
            <Field label="期望解析 JSON">
              <TextArea
                className="font-mono min-h-[190px]"
                value={expectedText}
                onChange={(e) => setExpectedText(e.target.value)}
                placeholder='{"symbol":"BTC/USDT","side":"long"}'
              />
            </Field>
            <Field label="备注">
              <Input value={note} onChange={(e) => setNote(e.target.value)} placeholder="这个用例覆盖什么场景" />
            </Field>
            <div className="flex gap-2">
              <Button onClick={saveCase} className="flex-1 justify-center">
                <Save size={14} /> {editingId ? "保存修改" : "新增用例"}
              </Button>
              <Button variant="ghost" onClick={resetForm}>清空</Button>
            </div>
          </div>
        </Card>

        <div className="xl:col-span-2 space-y-4">
          <Card>
            <CardTitle action={<Badge tone="default">共 {caseTotal} 条</Badge>}>用例列表</CardTitle>
            <div className="grid grid-cols-1 md:grid-cols-3 gap-3 mb-3">
              <Field label="搜索/标签">
                <Input
                  value={caseQ}
                  onChange={(e) => {
                    setCaseQ(e.target.value);
                    setCasePage(1);
                  }}
                  placeholder="高风险待确认 / BTC / 文件名"
                />
              </Field>
              <Field label="状态筛选">
                <Select
                  value={caseEnabledFilter}
                  onChange={(e) => {
                    setCaseEnabledFilter(e.target.value);
                    setCasePage(1);
                  }}
                >
                  <option value="all">全部</option>
                  <option value="enabled">只看启用</option>
                  <option value="disabled">只看草稿停用</option>
                </Select>
              </Field>
              <div className="flex items-end gap-2 flex-wrap">
                <Button variant="ghost" onClick={() => loadCases(casePage)} disabled={loading}>
                  <RefreshCw size={14} /> {loading ? "加载中..." : "刷新"}
                </Button>
                <Button variant="ghost" onClick={() => { setCaseQ("高风险待确认"); setCaseEnabledFilter("all"); setCasePage(1); }}>
                  只看高风险
                </Button>
                <Button variant="danger" onClick={() => bulkDeleteCases("filtered")}>
                  删除当前筛选
                </Button>
                <Button variant="danger" onClick={() => bulkDeleteCases("drafts")}>
                  删除全部草稿
                </Button>
                <Button variant="danger" onClick={() => bulkDeleteCases("all")}>
                  删除全部
                </Button>
              </div>
            </div>
            <div className="flex items-center justify-between gap-3 mb-3 text-xs text-text-muted">
              <span>第 {casePage} / {casePages} 页，每页 {casePageSize} 条，当前显示 {cases.length} 条</span>
              <div className="flex gap-2">
                <Button variant="ghost" disabled={casePage <= 1 || loading} onClick={() => setCasePage((p) => Math.max(1, p - 1))}>上一页</Button>
                <Button variant="ghost" disabled={casePage >= casePages || loading} onClick={() => setCasePage((p) => Math.min(casePages, p + 1))}>下一页</Button>
              </div>
            </div>
            {cases.length ? (
              <div className="space-y-2">
                {cases.map((item) => (
                  <div key={item.id} className="glass-soft p-3">
                    <div className="flex flex-col lg:flex-row lg:items-start justify-between gap-3">
                      <div className="min-w-0">
                        <div className="flex items-center gap-2 flex-wrap">
                          <span className="font-semibold text-text">#{item.id} {item.name}</span>
                          <Badge tone={item.enabled ? "profit" : "default"}>{item.enabled ? "启用" : "停用"}</Badge>
                          {item.tags && <Badge tone="accent">{item.tags}</Badge>}
                        </div>
                        <div className="text-xs text-text-tertiary mt-2 whitespace-pre-wrap break-words line-clamp-2">{item.raw_text}</div>
                        <div className="text-xs text-text-muted mt-2 font-mono">期望字段: {Object.keys(item.expected || {}).join(", ") || "未设置"}</div>
                      </div>
                      <div className="flex gap-2 shrink-0">
                        <Button variant="ghost" onClick={() => editCase(item)}>编辑</Button>
                        <Button variant="ghost" onClick={() => runCases([item.id])}><Play size={13} /> 运行</Button>
                        <Button variant="danger" onClick={() => deleteCase(item.id)}><Trash2 size={13} /></Button>
                      </div>
                    </div>
                  </div>
                ))}
              </div>
            ) : (
              <div className="text-sm text-text-tertiary py-10 text-center">
                当前筛选下暂无回归用例。可以调整搜索/状态筛选，或先在左侧保存几条典型 KOL 消息。
              </div>
            )}
          </Card>

          {runResult && (
            <Card>
              <CardTitle
                action={
                  runResult.failed ? <Badge tone="loss">失败 {runResult.failed}</Badge> : <Badge tone="profit">全部通过</Badge>
                }
              >
                最近一次运行结果
              </CardTitle>
              <div className="grid grid-cols-3 gap-3 mb-4">
                <div className="glass-soft p-3">
                  <div className="text-xs text-text-muted">总数</div>
                  <div className="text-lg font-mono text-text mt-1">{runResult.total}</div>
                </div>
                <div className="glass-soft p-3">
                  <div className="text-xs text-text-muted">通过</div>
                  <div className="text-lg font-mono text-profit mt-1">{runResult.passed}</div>
                </div>
                <div className="glass-soft p-3">
                  <div className="text-xs text-text-muted">失败</div>
                  <div className="text-lg font-mono text-loss mt-1">{runResult.failed}</div>
                </div>
              </div>
              <div className="space-y-3">
                {results.map((r) => (
                  <div key={r.case_id} className="rounded-xl border border-border/60 bg-bg-card/40 p-3">
                    <div className="flex items-center justify-between gap-2 flex-wrap">
                      <div className="font-semibold text-text">#{r.case_id} {r.name}</div>
                      <Badge tone={r.passed ? "profit" : "loss"}>{r.passed ? "通过" : "失败"}</Badge>
                    </div>
                    {r.failed_fields?.length ? (
                      <div className="mt-3 overflow-auto">
                        <table className="w-full text-xs">
                          <thead className="text-text-muted">
                            <tr className="border-b border-white/10">
                              <th className="py-2 text-left">字段</th>
                              <th className="py-2 text-left">期望</th>
                              <th className="py-2 text-left">实际</th>
                            </tr>
                          </thead>
                          <tbody>
                            {r.failed_fields.map((f: any) => (
                              <tr key={f.field} className="border-b border-white/5">
                                <td className="py-2 pr-3 font-mono text-gold">{f.field}</td>
                                <td className="py-2 pr-3 font-mono text-text-secondary">{JSON.stringify(f.expected)}</td>
                                <td className="py-2 pr-3 font-mono text-loss">{JSON.stringify(f.actual)}</td>
                              </tr>
                            ))}
                          </tbody>
                        </table>
                      </div>
                    ) : (
                      <div className="mt-2 text-xs text-text-tertiary">断言字段通过: {r.passed_fields?.join(", ") || "未设置断言字段"}</div>
                    )}
                    <details className="mt-3">
                      <summary className="cursor-pointer text-xs text-text-muted">查看实际解析 JSON</summary>
                      <div className="mt-2">
                        <JsonBlock data={r.actual} />
                      </div>
                    </details>
                  </div>
                ))}
              </div>
            </Card>
          )}
        </div>
      </div>
    </div>
  );
}

export default function AdminSimulator() {
  const [tab, setTab] = useState<"single" | "regression">("single");
  return (
    <div className="space-y-4">
      <div className="flex flex-col sm:flex-row sm:items-center justify-between gap-3">
        <div>
          <h1 className="text-xl font-bold gradient-text flex items-center gap-2 font-display">
            <FlaskConical size={20} /> KOL 信号测试
          </h1>
          <p className="text-sm text-text-tertiary mt-1">单消息用于临时调试；回归测试用于保存典型样本并批量验证 parser 是否退化。</p>
        </div>
        <Badge tone="profit"><ShieldCheck size={12} /> 安全测试，不写订单</Badge>
      </div>
      <div className="flex gap-2 flex-wrap">
        <button
          onClick={() => setTab("single")}
          className={`px-3 py-2 rounded-xl text-sm font-semibold border transition-all ${
            tab === "single" ? "bg-emerald/[0.09] text-emerald border-emerald-border" : "glass-soft text-text-tertiary border-border/60 hover:text-text"
          }`}
        >
          单消息测试
        </button>
        <button
          onClick={() => setTab("regression")}
          className={`px-3 py-2 rounded-xl text-sm font-semibold border transition-all ${
            tab === "regression" ? "bg-emerald/[0.09] text-emerald border-emerald-border" : "glass-soft text-text-tertiary border-border/60 hover:text-text"
          }`}
        >
          回归测试
        </button>
      </div>
      {tab === "single" ? <SingleMessagePanel /> : <RegressionPanel />}
    </div>
  );
}
