import { useEffect, useMemo, useState } from "react";
import {
  Download,
  TrendingUp,
  Receipt,
  Coins,
  Users,
  ArrowUpDown,
  ArrowUp,
  ArrowDown,
  Loader2,
  UserCheck,
  Percent,
} from "lucide-react";
import { API } from "@/api/client";
import { useAuthStore } from "@/stores/auth";
import { useToast } from "@/components/ui/Toast";
import { Card, Badge, Button, Input, Select, Field, Empty } from "@/components/ui";
import { fmtMoney, pnlColor } from "@/lib/utils";

interface Summary {
  total_pnl: number;
  total_fee: number;
  total_commission: number;
  total_share: number;
  active_count: number;
}

// 后端返回的字段名,必须与 analytics.customer_profit_stats 保持一致
interface CustomerProfit {
  customer_id: number;
  username: string;
  display_name?: string;
  customer_type?: string; // normal | internal
  total_pnl: number;
  total_fee: number;
  trade_count: number;
  commission_earned: number;
  invited_count: number;
  inviter_name?: string;
}

type SortDir = "asc" | "desc";

// 根据快速选择计算日期区间(YYYY-MM-DD)
function getDateRange(range: string): { start_date: string; end_date: string } {
  const now = new Date();
  const fmt = (d: Date) => {
    const y = d.getFullYear();
    const m = String(d.getMonth() + 1).padStart(2, "0");
    const day = String(d.getDate()).padStart(2, "0");
    return `${y}-${m}-${day}`;
  };
  const end_date = fmt(now);
  if (range === "today") {
    return { start_date: end_date, end_date };
  }
  if (range === "week") {
    const day = now.getDay() || 7; // 周日=0 -> 7
    const monday = new Date(now);
    monday.setDate(now.getDate() - day + 1);
    return { start_date: fmt(monday), end_date };
  }
  if (range === "month") {
    const first = new Date(now.getFullYear(), now.getMonth(), 1);
    return { start_date: fmt(first), end_date };
  }
  return { start_date: "", end_date: "" };
}

export default function ProfitStatsPage() {
  const { push } = useToast();
  const [range, setRange] = useState("today"); // today/week/month/custom
  const [startDate, setStartDate] = useState("");
  const [endDate, setEndDate] = useState("");
  const [customerType, setCustomerType] = useState("");
  // 利润分成百分比(0=不计算)
  const [pctInput, setPctInput] = useState(""); // 文本输入框的值
  const pct = useMemo(() => {
    const n = parseFloat(pctInput);
    return isNaN(n) || n <= 0 ? 0 : Math.min(n, 100);
  }, [pctInput]);

  const [data, setData] = useState<CustomerProfit[]>([]);
  const [summary, setSummary] = useState<Summary>({
    total_pnl: 0,
    total_fee: 0,
    total_commission: 0,
    total_share: 0,
    active_count: 0,
  });
  const [loading, setLoading] = useState(false);
  const [exporting, setExporting] = useState(false);
  const [sortKey, setSortKey] = useState<string>("total_pnl");
  const [sortDir, setSortDir] = useState<SortDir>("desc");

  // 快速选择时自动计算日期
  useEffect(() => {
    if (range !== "custom") {
      const r = getDateRange(range);
      setStartDate(r.start_date);
      setEndDate(r.end_date);
    }
  }, [range]);

  // 加载数据
  useEffect(() => {
    if (!startDate || !endDate) return;
    let cancelled = false;
    const load = async () => {
      setLoading(true);
      try {
        const res: any = await API.getProfitStats({
          start_date: startDate,
          end_date: endDate,
          customer_type: customerType || undefined,
        });
        if (cancelled) return;
        const rows: CustomerProfit[] = Array.isArray(res) ? res : (res?.data || []);
        setData(rows);
        // 前端计算汇总
        const total_pnl = rows.reduce((s, r) => s + Number(r.total_pnl || 0), 0);
        const total_fee = rows.reduce((s, r) => s + Number(r.total_fee || 0), 0);
        const total_commission = rows.reduce((s, r) => s + Number(r.commission_earned || 0), 0);
        const total_share = pct > 0 ? total_pnl * pct / 100 : 0;
        const active_count = rows.filter((r) => r.trade_count > 0).length;
        setSummary({ total_pnl, total_fee, total_commission, total_share, active_count });
      } catch (e: any) {
        if (!cancelled) push("error", e?.response?.data?.message || "加载失败");
      } finally {
        if (!cancelled) setLoading(false);
      }
    };
    load();
    return () => { cancelled = true; };
  }, [startDate, endDate, customerType, pct]);

  const toggleSort = (key: string) => {
    if (sortKey === key) {
      setSortDir((d) => (d === "asc" ? "desc" : "asc"));
    } else {
      setSortKey(key);
      setSortDir("desc");
    }
  };

  // 计算每行的分成金额
  const rowsWithShare = useMemo(() => {
    return data.map((r) => ({
      ...r,
      share_amount: pct > 0 ? Number(r.total_pnl) * pct / 100 : 0,
      net_pnl: Number(r.total_pnl) - Number(r.total_fee),
    }));
  }, [data, pct]);

  const sorted = useMemo(() => {
    const arr = [...rowsWithShare];
    arr.sort((a: any, b: any) => {
      if (sortKey === "username") {
        const cmp = (a.username || "").localeCompare(b.username || "");
        return sortDir === "asc" ? cmp : -cmp;
      }
      if (sortKey === "share_amount") {
        const av = Number(a.share_amount ?? 0);
        const bv = Number(b.share_amount ?? 0);
        return sortDir === "asc" ? av - bv : bv - av;
      }
      const av = Number(a[sortKey] ?? 0);
      const bv = Number(b[sortKey] ?? 0);
      return sortDir === "asc" ? av - bv : bv - av;
    });
    return arr;
  }, [rowsWithShare, sortKey, sortDir]);

  const exportExcel = async () => {
    if (!startDate || !endDate) {
      push("error", "请先选择日期范围");
      return;
    }
    setExporting(true);
    try {
      const url = API.exportProfitStats({
        start_date: startDate,
        end_date: endDate,
        customer_type: customerType || undefined,
        profit_percentage: pct > 0 ? pct : undefined,
      });
      const token = useAuthStore.getState().token;
      const res = await fetch(url, {
        headers: token ? { Authorization: `Bearer ${token}` } : {},
      });
      if (!res.ok) throw new Error(`HTTP ${res.status}`);
      const blob = await res.blob();
      const a = document.createElement("a");
      a.href = URL.createObjectURL(blob);
      const tag = pct > 0 ? `_${pct}pct` : "";
      a.download = `利润统计_${startDate}_${endDate}${tag}.xlsx`;
      document.body.appendChild(a);
      a.click();
      document.body.removeChild(a);
      URL.revokeObjectURL(a.href);
      push("success", "导出成功");
    } catch (e: any) {
      push("error", e?.message || "导出失败");
    } finally {
      setExporting(false);
    }
  };

  const SortIcon = ({ col }: { col: string }) => {
    if (sortKey !== col)
      return <ArrowUpDown size={12} className="text-slate-600 inline ml-1" />;
    return sortDir === "asc" ? (
      <ArrowUp size={12} className="text-accent-glow inline ml-1" />
    ) : (
      <ArrowDown size={12} className="text-accent-glow inline ml-1" />
    );
  };

  const rangeOptions = [
    { value: "today", label: "今日" },
    { value: "week", label: "本周" },
    { value: "month", label: "本月" },
    { value: "custom", label: "自定义" },
  ];

  const pctPresets = [10, 30, 50, 100];

  return (
    <div className="space-y-6">
      {/* 顶部标题 + 导出 */}
      <div className="flex flex-col sm:flex-row sm:items-center justify-between gap-3">
        <div>
          <h1 className="text-xl font-bold gradient-text flex items-center gap-2">
            <TrendingUp size={20} /> 利润统计
          </h1>
          <p className="text-sm text-slate-500 mt-1">
            按时间段统计各客户利润、手续费与佣金,支持自定义分成百分比与导出 Excel
          </p>
        </div>
        <div className="flex items-center gap-2">
          <Button variant="ghost" onClick={() => { setRange("today"); }} disabled={loading}>
            刷新
          </Button>
          <Button onClick={exportExcel} disabled={exporting || loading || data.length === 0}>
            {exporting ? <Loader2 size={15} className="animate-spin" /> : <Download size={15} />}
            {exporting ? "导出中..." : "导出 Excel"}
          </Button>
        </div>
      </div>

      {/* 时间筛选栏 */}
      <Card>
        <div className="flex flex-col lg:flex-row lg:items-end gap-3 lg:gap-4">
          <Field label="快速选择">
            <div className="flex flex-wrap gap-1.5">
              {rangeOptions.map((opt) => (
                <button
                  key={opt.value}
                  onClick={() => setRange(opt.value)}
                  className={`px-3 py-2 rounded-lg text-xs font-semibold transition border ${
                    range === opt.value
                      ? "bg-gold/15 text-gold border-gold/30"
                      : "bg-bg-hover/60 text-slate-400 border-border-soft/60 hover:text-slate-200"
                  }`}
                >
                  {opt.label}
                </button>
              ))}
            </div>
          </Field>

          {range === "custom" && (
            <div className="grid grid-cols-2 gap-3">
              <Field label="开始日期">
                <Input type="date" value={startDate} onChange={(e) => setStartDate(e.target.value)} />
              </Field>
              <Field label="结束日期">
                <Input type="date" value={endDate} onChange={(e) => setEndDate(e.target.value)} />
              </Field>
            </div>
          )}

          <Field label="客户分类">
            <Select value={customerType} onChange={(e) => setCustomerType(e.target.value)} className="lg:w-44">
              <option value="">全部客户</option>
              <option value="normal">普通客户</option>
              <option value="internal">内部用户</option>
            </Select>
          </Field>

          {/* 利润分成百分比 */}
          <Field label="利润分成 %">
            <div className="flex items-center gap-2">
              <div className="flex flex-wrap gap-1.5">
                {pctPresets.map((p) => (
                  <button
                    key={p}
                    onClick={() => setPctInput(String(p))}
                    className={`px-2.5 py-1.5 rounded-lg text-xs font-semibold transition border ${
                      pct === p
                        ? "bg-sky-500/15 text-sky-400 border-sky-400/30"
                        : "bg-bg-hover/60 text-slate-400 border-border-soft/60 hover:text-slate-200"
                    }`}
                  >
                    {p}%
                  </button>
                ))}
              </div>
              <Input
                type="number"
                min="0"
                max="100"
                step="1"
                value={pctInput}
                onChange={(e) => setPctInput(e.target.value)}
                placeholder="自定义"
                className="w-20 text-center"
              />
              {pct > 0 && (
                <button
                  onClick={() => setPctInput("")}
                  className="text-xs text-slate-500 hover:text-loss transition"
                  title="清除百分比"
                >
                  清除
                </button>
              )}
            </div>
          </Field>

          <div className="lg:ml-auto text-xs text-slate-500 lg:pb-2">
            统计区间: <span className="text-slate-300 font-mono">{startDate || "—"}</span> ~{" "}
            <span className="text-slate-300 font-mono">{endDate || "—"}</span>
          </div>
        </div>
      </Card>

      {/* 汇总卡片 */}
      <div className={`grid gap-3 ${pct > 0 ? "grid-cols-2 lg:grid-cols-5" : "grid-cols-2 lg:grid-cols-4"}`}>
        <div className="glass-soft p-4">
          <div className="flex items-center justify-between mb-1.5">
            <span className="text-xs text-slate-500">总利润</span>
            <TrendingUp size={16} className="text-slate-600" />
          </div>
          <div className={`text-xl md:text-2xl font-bold font-mono ${pnlColor(summary.total_pnl)}`}>
            {fmtMoney(summary.total_pnl)}
          </div>
          <div className="text-[11px] text-slate-500 mt-1">所有客户 PnL 之和</div>
        </div>
        <div className="glass-soft p-4">
          <div className="flex items-center justify-between mb-1.5">
            <span className="text-xs text-slate-500">总手续费</span>
            <Receipt size={16} className="text-slate-600" />
          </div>
          <div className="text-xl md:text-2xl font-bold font-mono text-slate-100">
            {fmtMoney(summary.total_fee)}
          </div>
          <div className="text-[11px] text-slate-500 mt-1">交易手续费合计</div>
        </div>
        <div className="glass-soft p-4">
          <div className="flex items-center justify-between mb-1.5">
            <span className="text-xs text-slate-500">总佣金支出</span>
            <Coins size={16} className="text-slate-600" />
          </div>
          <div className="text-xl md:text-2xl font-bold font-mono text-amber-400">
            {fmtMoney(summary.total_commission)}
          </div>
          <div className="text-[11px] text-slate-500 mt-1">下级利润的10%提成</div>
        </div>
        {pct > 0 && (
          <div className="glass-soft p-4 ring-1 ring-sky-400/20">
            <div className="flex items-center justify-between mb-1.5">
              <span className="text-xs text-slate-500">分成总额 ({pct}%)</span>
              <Percent size={16} className="text-sky-400" />
            </div>
            <div className="text-xl md:text-2xl font-bold font-mono text-sky-400">
              {fmtMoney(summary.total_share)}
            </div>
            <div className="text-[11px] text-slate-500 mt-1">总利润 × {pct}%</div>
          </div>
        )}
        <div className="glass-soft p-4">
          <div className="flex items-center justify-between mb-1.5">
            <span className="text-xs text-slate-500">活跃客户数</span>
            <Users size={16} className="text-slate-600" />
          </div>
          <div className="text-xl md:text-2xl font-bold font-mono text-accent-glow">
            {summary.active_count}
          </div>
          <div className="text-[11px] text-slate-500 mt-1">区间内有交易的客户</div>
        </div>
      </div>

      {/* 客户利润表格 */}
      <Card>
        <div className="flex items-center justify-between mb-4">
          <h3 className="text-base font-bold text-slate-100 tracking-tight">
            客户利润明细
            {pct > 0 && <span className="ml-2 text-sm text-sky-400 font-normal">({pct}% 分成)</span>}
          </h3>
          {loading && (
            <span className="text-xs text-slate-500 flex items-center gap-1.5">
              <Loader2 size={13} className="animate-spin" /> 加载中...
            </span>
          )}
        </div>

        {data.length === 0 && !loading ? (
          <Empty text="该区间暂无利润数据" />
        ) : (
          <>
            {/* 桌面端表格 */}
            <div className="hidden md:block overflow-x-auto">
              <table className="w-full text-sm">
                <thead>
                  <tr className="text-xs text-slate-500 border-b border-border">
                    <th className="text-left py-3 px-3 cursor-pointer select-none hover:text-slate-300" onClick={() => toggleSort("username")}>
                      客户名<SortIcon col="username" />
                    </th>
                    <th className="text-center px-3">类型</th>
                    <th className="text-right px-3 cursor-pointer select-none hover:text-slate-300" onClick={() => toggleSort("total_pnl")}>
                      总利润<SortIcon col="total_pnl" />
                    </th>
                    <th className="text-right px-3 cursor-pointer select-none hover:text-slate-300" onClick={() => toggleSort("total_fee")}>
                      手续费<SortIcon col="total_fee" />
                    </th>
                    <th className="text-right px-3 cursor-pointer select-none hover:text-slate-300" onClick={() => toggleSort("trade_count")}>
                      平仓数<SortIcon col="trade_count" />
                    </th>
                    {pct > 0 && (
                      <th className="text-right px-3 cursor-pointer select-none hover:text-slate-300" onClick={() => toggleSort("share_amount")}>
                        分成({pct}%)<SortIcon col="share_amount" />
                      </th>
                    )}
                    <th className="text-right px-3 cursor-pointer select-none hover:text-slate-300" onClick={() => toggleSort("commission_earned")}>
                      佣金收入<SortIcon col="commission_earned" />
                    </th>
                    <th className="text-center px-3 cursor-pointer select-none hover:text-slate-300" onClick={() => toggleSort("invited_count")}>
                      邀请人数<SortIcon col="invited_count" />
                    </th>
                    <th className="text-left px-3">邀请人</th>
                  </tr>
                </thead>
                <tbody>
                  {sorted.map((c: any) => {
                    const isInternal = c.customer_type === "internal";
                    return (
                      <tr key={c.customer_id} className="border-b border-border/50 hover:bg-bg-hover/40">
                        <td className="py-3 px-3">
                          <div className={`font-medium ${isInternal ? "text-amber-400" : "text-slate-100"}`}>
                            {c.display_name || c.username}
                          </div>
                          <div className="text-[11px] text-slate-500 font-mono">{c.username}</div>
                        </td>
                        <td className="px-3 text-center">
                          <Badge tone={isInternal ? "gold" : "default"}>
                            {isInternal ? "内部用户" : "普通客户"}
                          </Badge>
                        </td>
                        <td className={`px-3 text-right font-mono ${pnlColor(c.total_pnl)}`}>
                          {fmtMoney(c.total_pnl)}
                        </td>
                        <td className="px-3 text-right font-mono text-slate-300">
                          {fmtMoney(c.total_fee)}
                        </td>
                        <td className="px-3 text-right font-mono text-slate-300">
                          {c.trade_count}
                        </td>
                        {pct > 0 && (
                          <td className="px-3 text-right font-mono text-sky-400 font-semibold">
                            {fmtMoney(c.share_amount)}
                          </td>
                        )}
                        <td className="px-3 text-right font-mono text-amber-400">
                          {fmtMoney(c.commission_earned)}
                        </td>
                        <td className="px-3 text-center">
                          <span className="inline-flex items-center gap-1 text-slate-300">
                            <UserCheck size={12} className="text-slate-500" />
                            {c.invited_count}
                          </span>
                        </td>
                        <td className="px-3 text-slate-300 text-xs">
                          {c.inviter_name || "—"}
                        </td>
                      </tr>
                    );
                  })}
                </tbody>
                {/* 合计行 */}
                {sorted.length > 0 && (
                  <tfoot>
                    <tr className="border-t-2 border-border font-semibold">
                      <td className="py-3 px-3 text-slate-300" colSpan={2}>合计</td>
                      <td className={`px-3 text-right font-mono ${pnlColor(summary.total_pnl)}`}>
                        {fmtMoney(summary.total_pnl)}
                      </td>
                      <td className="px-3 text-right font-mono text-slate-300">
                        {fmtMoney(summary.total_fee)}
                      </td>
                      <td className="px-3 text-right font-mono text-slate-300">
                        {sorted.reduce((s, c: any) => s + (c.trade_count || 0), 0)}
                      </td>
                      {pct > 0 && (
                        <td className="px-3 text-right font-mono text-sky-400">
                          {fmtMoney(summary.total_share)}
                        </td>
                      )}
                      <td className="px-3 text-right font-mono text-amber-400">
                        {fmtMoney(summary.total_commission)}
                      </td>
                      <td className="px-3 text-center text-slate-300">
                        {sorted.reduce((s, c: any) => s + (c.invited_count || 0), 0)}
                      </td>
                      <td className="px-3"></td>
                    </tr>
                  </tfoot>
                )}
              </table>
            </div>

            {/* 移动端卡片列表 */}
            <div className="md:hidden space-y-3">
              {sorted.map((c: any) => {
                const isInternal = c.customer_type === "internal";
                const net = Number(c.total_pnl) - Number(c.total_fee);
                return (
                  <div key={c.customer_id} className="glass-soft p-4 glow-border">
                    <div className="flex items-center justify-between mb-3">
                      <div className="min-w-0">
                        <div className={`font-semibold truncate ${isInternal ? "text-amber-400" : "text-slate-100"}`}>
                          {c.display_name || c.username}
                        </div>
                        <div className="text-[11px] text-slate-500 font-mono">{c.username}</div>
                      </div>
                      <Badge tone={isInternal ? "gold" : "default"}>
                        {isInternal ? "内部用户" : "普通客户"}
                      </Badge>
                    </div>
                    <div className="grid grid-cols-2 gap-2 text-xs">
                      <div>
                        <div className="text-slate-500 text-[11px]">总利润</div>
                        <div className={`font-mono ${pnlColor(c.total_pnl)}`}>{fmtMoney(c.total_pnl)}</div>
                      </div>
                      <div>
                        <div className="text-slate-500 text-[11px]">手续费</div>
                        <div className="font-mono text-slate-300">{fmtMoney(c.total_fee)}</div>
                      </div>
                      <div>
                        <div className="text-slate-500 text-[11px]">净利润</div>
                        <div className={`font-mono ${pnlColor(net)}`}>{fmtMoney(net)}</div>
                      </div>
                      {pct > 0 && (
                        <div>
                          <div className="text-slate-500 text-[11px]">分成({pct}%)</div>
                          <div className="font-mono text-sky-400 font-semibold">{fmtMoney(c.share_amount)}</div>
                        </div>
                      )}
                      <div>
                        <div className="text-slate-500 text-[11px]">平仓数</div>
                        <div className="text-slate-300">{c.trade_count}</div>
                      </div>
                      <div>
                        <div className="text-slate-500 text-[11px]">佣金收入</div>
                        <div className="font-mono text-amber-400">{fmtMoney(c.commission_earned)}</div>
                      </div>
                      <div>
                        <div className="text-slate-500 text-[11px]">邀请人数</div>
                        <div className="text-slate-300">{c.invited_count}</div>
                      </div>
                      <div>
                        <div className="text-slate-500 text-[11px]">邀请人</div>
                        <div className="text-slate-300 truncate">{c.inviter_name || "—"}</div>
                      </div>
                    </div>
                  </div>
                );
              })}
              {/* 移动端合计 */}
              {sorted.length > 0 && (
                <div className="glass p-4 border-t-2 border-border">
                  <div className="text-sm font-bold text-slate-200 mb-2">合计</div>
                  <div className="grid grid-cols-2 gap-2 text-xs">
                    <div>
                      <span className="text-slate-500">总利润: </span>
                      <span className={`font-mono ${pnlColor(summary.total_pnl)}`}>{fmtMoney(summary.total_pnl)}</span>
                    </div>
                    {pct > 0 && (
                      <div>
                        <span className="text-slate-500">分成({pct}%): </span>
                        <span className="font-mono text-sky-400 font-semibold">{fmtMoney(summary.total_share)}</span>
                      </div>
                    )}
                    <div>
                      <span className="text-slate-500">佣金: </span>
                      <span className="font-mono text-amber-400">{fmtMoney(summary.total_commission)}</span>
                    </div>
                  </div>
                </div>
              )}
            </div>
          </>
        )}
      </Card>
    </div>
  );
}
