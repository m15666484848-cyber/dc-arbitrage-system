import { useEffect, useMemo, useState } from "react";
import { AlertTriangle, CheckCircle2, GitCompareArrows, RefreshCw, ShieldCheck, XCircle } from "lucide-react";
import { API } from "@/api/client";
import { Badge, Button, Card, CardTitle, Empty, Field, Input, MetricCard, Select } from "@/components/ui";
import { useToast } from "@/components/ui/Toast";
import { fmtMoney, fmtTime } from "@/lib/utils";

type ShadowStatus = "pending" | "accepted" | "rejected" | "ignored";

const statusTone: Record<string, "default" | "profit" | "loss" | "warn" | "accent"> = {
  pending: "warn",
  accepted: "profit",
  rejected: "loss",
  ignored: "default",
};

const statusLabel: Record<string, string> = {
  pending: "待审核",
  accepted: "已采纳",
  rejected: "已拒绝",
  ignored: "已忽略",
};

function JsonBlock({ data }: { data: any }) {
  const text = useMemo(() => JSON.stringify(data || {}, null, 2), [data]);
  return (
    <pre className="text-xs leading-5 overflow-auto max-h-[320px] rounded-xl bg-black/30 border border-white/10 p-3 text-slate-300">
      {text}
    </pre>
  );
}

function ValueCell({ value, changed }: { value: any; changed: boolean }) {
  const display =
    value === null || value === undefined || value === ""
      ? "—"
      : typeof value === "number"
        ? fmtMoney(value, 6)
        : String(value);
  return (
    <td className={`py-2 px-3 font-mono text-xs ${changed ? "text-gold" : "text-text-secondary"}`}>
      {display}
    </td>
  );
}

function CompareTable({ item }: { item: any }) {
  const rows = [
    ["状态", item.old_status, item.new_status],
    ["品种", item.old_symbol, item.new_symbol],
    ["方向", item.old_side, item.new_side],
    ["入场", item.old_entry_price, item.new_entry_price],
    ["止损", item.old_stop_loss, item.new_stop_loss],
  ];
  return (
    <div className="overflow-auto">
      <table className="w-full text-sm">
        <thead className="text-text-muted">
          <tr className="border-b border-white/10">
            <th className="py-2 px-3 text-left">字段</th>
            <th className="py-2 px-3 text-left">旧解析</th>
            <th className="py-2 px-3 text-left">新解析</th>
          </tr>
        </thead>
        <tbody>
          {rows.map(([label, oldValue, newValue]) => {
            const changed = String(oldValue ?? "") !== String(newValue ?? "");
            return (
              <tr key={label as string} className="border-b border-white/5">
                <td className="py-2 px-3 text-text-muted">{label}</td>
                <ValueCell value={oldValue} changed={changed} />
                <ValueCell value={newValue} changed={changed} />
              </tr>
            );
          })}
        </tbody>
      </table>
    </div>
  );
}

export default function ShadowComparison() {
  const { push } = useToast();
  const [hours, setHours] = useState("168");
  const [kolId, setKolId] = useState("");
  const [status, setStatus] = useState("");
  const [mismatchOnly, setMismatchOnly] = useState(true);
  const [page, setPage] = useState(1);
  const [loading, setLoading] = useState(false);
  const [data, setData] = useState<any>(null);
  const [kols, setKols] = useState<any[]>([]);
  const [reviewNote, setReviewNote] = useState<Record<number, string>>({});

  const load = async () => {
    setLoading(true);
    try {
      const res = await API.listShadowResults({
        page,
        page_size: 30,
        hours: Number(hours || 168),
        kol_id: kolId ? Number(kolId) : undefined,
        status: status || undefined,
        mismatch_only: mismatchOnly,
      });
      setData(res);
    } catch (e: any) {
      push("error", e?.response?.data?.detail || e?.response?.data?.message || "加载影子解析结果失败");
    } finally {
      setLoading(false);
    }
  };

  useEffect(() => {
    API.listAdminKols().then((rows: any) => setKols(rows || [])).catch(() => setKols([]));
  }, []);

  useEffect(() => {
    load();
  }, [page, hours, kolId, status, mismatchOnly]);

  const items: any[] = data?.items || [];
  const summary = data?.summary || {};
  const total = data?.total || 0;
  const totalPages = Math.max(Math.ceil(total / 30), 1);

  const review = async (id: number, nextStatus: ShadowStatus) => {
    try {
      await API.reviewShadowResult(id, {
        status: nextStatus,
        review_note: reviewNote[id] || "",
      });
      push("success", "审核状态已更新");
      load();
    } catch (e: any) {
      push("error", e?.response?.data?.detail || e?.response?.data?.message || "审核失败");
    }
  };

  return (
    <div className="space-y-5 md:space-y-6">
      <div className="flex flex-col lg:flex-row lg:items-center justify-between gap-3">
        <div>
          <h1 className="text-xl font-bold gradient-text flex items-center gap-2">
            <GitCompareArrows size={20} />
            影子解析对比
          </h1>
          <p className="text-sm text-text-tertiary mt-1">新解析器旁路运行，只记录对比结果，不影响真实订单。</p>
        </div>
        <div className="flex items-center gap-2 flex-wrap">
          <Badge tone="profit"><ShieldCheck size={12} /> 影子模式安全</Badge>
          <Button variant="ghost" onClick={load} disabled={loading}>
            <RefreshCw size={15} /> {loading ? "刷新中..." : "刷新"}
          </Button>
        </div>
      </div>

      <div className="grid grid-cols-2 lg:grid-cols-4 gap-3">
        <MetricCard label="结果总数" value={total} sub="当前筛选范围" icon={GitCompareArrows} />
        <MetricCard label="待审核" value={summary.pending || 0} tone="gold" sub="需要人工看差异" icon={AlertTriangle} />
        <MetricCard label="有差异" value={summary.mismatch || 0} tone="loss" sub="新旧解析不一致" icon={XCircle} />
        <MetricCard label="一致结果" value={summary.matched || 0} tone="profit" sub="新旧核心字段一致" icon={CheckCircle2} />
      </div>

      <Card>
        <div className="grid grid-cols-1 md:grid-cols-5 gap-3">
          <Field label="时间范围(小时)">
            <Input type="number" min={1} max={2160} value={hours} onChange={(e) => { setPage(1); setHours(e.target.value); }} />
          </Field>
          <Field label="KOL">
            <Select value={kolId} onChange={(e) => { setPage(1); setKolId(e.target.value); }}>
              <option value="">全部 KOL</option>
              {kols.map((k) => (
                <option key={k.id} value={k.id}>{k.name}</option>
              ))}
            </Select>
          </Field>
          <Field label="审核状态">
            <Select value={status} onChange={(e) => { setPage(1); setStatus(e.target.value); }}>
              <option value="">全部状态</option>
              <option value="pending">待审核</option>
              <option value="accepted">已采纳</option>
              <option value="rejected">已拒绝</option>
              <option value="ignored">已忽略</option>
            </Select>
          </Field>
          <Field label="差异筛选">
            <Select value={mismatchOnly ? "yes" : "no"} onChange={(e) => { setPage(1); setMismatchOnly(e.target.value === "yes"); }}>
              <option value="yes">只看有差异</option>
              <option value="no">全部结果</option>
            </Select>
          </Field>
          <div className="flex items-end text-xs text-text-tertiary">
            第 <span className="mx-1 text-text-secondary font-mono">{page}</span> / <span className="mx-1 text-text-secondary font-mono">{totalPages}</span> 页
          </div>
        </div>
      </Card>

      {items.length === 0 ? (
        <Card>
          <Empty text={loading ? "加载中..." : "暂无影子解析结果。部署第一步后，第二步接入影子解析才会开始产生数据。"} />
        </Card>
      ) : (
        <div className="space-y-4">
          {items.map((item) => (
            <Card key={item.id}>
              <CardTitle
                action={
                  <Badge tone={statusTone[item.status] || "default"}>
                    {statusLabel[item.status] || item.status}
                  </Badge>
                }
              >
                #{item.id} {item.kol_name || `KOL ${item.kol_id || "未知"}`} · {fmtTime(item.created_at)}
              </CardTitle>

              <div className="grid grid-cols-1 xl:grid-cols-3 gap-4">
                <div className="xl:col-span-1 space-y-3">
                  <div className="glass-soft p-3">
                    <div className="text-xs text-text-muted mb-1">原始消息</div>
                    <div className="text-sm text-text-secondary whitespace-pre-wrap break-words max-h-[180px] overflow-auto">
                      {item.raw_text || "无文本"}
                    </div>
                  </div>
                  <div className="flex flex-wrap gap-2">
                    {(item.mismatch_fields || []).length ? (
                      item.mismatch_fields.map((f: string) => (
                        <Badge key={f} tone="warn">{f}</Badge>
                      ))
                    ) : (
                      <Badge tone="profit">核心字段一致</Badge>
                    )}
                  </div>
                  <div className="grid grid-cols-2 gap-2 text-xs">
                    <div className="glass-soft p-3">
                      <div className="text-text-muted">消息时间</div>
                      <div className="text-text-secondary mt-1">{fmtTime(item.signal_received_at)}</div>
                    </div>
                    <div className="glass-soft p-3">
                      <div className="text-text-muted">解析版本</div>
                      <div className="text-text-secondary mt-1 font-mono">{item.parse_version || "未标记"}</div>
                    </div>
                  </div>
                </div>

                <div className="xl:col-span-2 space-y-4">
                  <CompareTable item={item} />
                  <div className="grid grid-cols-1 lg:grid-cols-2 gap-3">
                    <div>
                      <div className="text-xs text-text-muted mb-2">旧解析 JSON</div>
                      <JsonBlock data={item.old_parsed} />
                    </div>
                    <div>
                      <div className="text-xs text-text-muted mb-2">新解析 JSON</div>
                      <JsonBlock data={item.new_parsed} />
                    </div>
                  </div>
                  <div className="flex flex-col lg:flex-row gap-2 lg:items-center">
                    <Input
                      value={reviewNote[item.id] || item.review_note || ""}
                      onChange={(e) => setReviewNote({ ...reviewNote, [item.id]: e.target.value })}
                      placeholder="审核备注，例如：新解析正确，旧解析漏了止损"
                    />
                    <div className="flex gap-2 flex-wrap">
                      <Button variant="primary" onClick={() => review(item.id, "accepted")}>采纳新解析</Button>
                      <Button variant="danger" onClick={() => review(item.id, "rejected")}>拒绝新解析</Button>
                      <Button variant="ghost" onClick={() => review(item.id, "ignored")}>忽略</Button>
                    </div>
                  </div>
                </div>
              </div>
            </Card>
          ))}
        </div>
      )}

      <div className="flex justify-end gap-2">
        <Button variant="ghost" disabled={page <= 1 || loading} onClick={() => setPage((p) => Math.max(p - 1, 1))}>上一页</Button>
        <Button variant="ghost" disabled={page >= totalPages || loading} onClick={() => setPage((p) => p + 1)}>下一页</Button>
      </div>
    </div>
  );
}
