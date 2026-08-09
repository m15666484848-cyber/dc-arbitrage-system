import { useEffect, useState } from "react";
import {
  AlertTriangle,
  CheckCircle2,
  Clock,
  FileSearch,
  RefreshCw,
  Radio,
  ShoppingCart,
  XCircle,
} from "lucide-react";
import { API } from "@/api/client";
import { useFetch } from "@/lib/useFetch";
import { Badge, Button, Card, CardTitle, Empty, Input, MetricCard, Select } from "@/components/ui";
import { fmtMoney, fmtTime, orderStatusLabel, sideLabel, signalStatusLabel } from "@/lib/utils";

const signalTone: Record<string, "default" | "profit" | "loss" | "warn" | "accent"> = {
  received: "default",
  parsed: "accent",
  corrected: "warn",
  filtered: "warn",
  ordered: "profit",
  rejected: "loss",
  ignored: "default",
};

const orderTone: Record<string, "default" | "profit" | "loss" | "warn" | "accent"> = {
  filled: "profit",
  partial: "warn",
  pending: "accent",
  cancelled: "default",
  deleted: "default",
  failed: "loss",
};

function ReasonBox({ text }: { text?: string }) {
  if (!text) return null;
  return (
    <div className="mt-2 rounded-xl border border-gold/20 bg-gold/10 px-3 py-2 text-xs text-gold whitespace-pre-wrap break-words">
      <AlertTriangle size={13} className="inline mr-1" />
      {text}
    </div>
  );
}

function OrderMini({ order }: { order: any }) {
  return (
    <div className="rounded-xl border border-border/60 bg-bg-card/40 px-3 py-2 text-xs">
      <div className="flex items-center gap-2 flex-wrap">
        <span className="font-semibold text-text-secondary">{order.customer_name}</span>
        <Badge tone={orderTone[order.status] || "default"} className="text-[10px]">{orderStatusLabel(order.status)}</Badge>
        <span className="text-text-muted">{order.exchange}</span>
        <span className="text-text-muted">{fmtTime(order.created_at)}</span>
      </div>
      <div className="mt-1 text-text-tertiary">
        {sideLabel(order.side)} {order.symbol} · 数量 {fmtMoney(order.qty, 6)}
        {order.price ? ` · 价格 ${fmtMoney(order.price, 4)}` : ""}
      </div>
      {order.error_msg && (
        <div className="mt-1 text-loss whitespace-pre-wrap break-words">失败原因: {order.error_msg}</div>
      )}
    </div>
  );
}

export default function AdminDiagnosis() {
  const [hours, setHours] = useState("24");
  const [signalStatus, setSignalStatus] = useState("");
  const [orderStatus, setOrderStatus] = useState("");
  const [customerId, setCustomerId] = useState("");
  const [kolId, setKolId] = useState("");
  const [tab, setTab] = useState<"signals" | "orders" | "audit">("signals");

  const params = {
    hours: Number(hours || 24),
    limit: 120,
    signal_status: signalStatus || undefined,
    order_status: orderStatus || undefined,
    customer_id: customerId ? Number(customerId) : undefined,
    kol_id: kolId ? Number(kolId) : undefined,
  };

  const { data, reload } = useFetch(() => API.getFollowDiagnosis(params), [
    hours,
    signalStatus,
    orderStatus,
    customerId,
    kolId,
  ]);
  const { data: sourceStatusData, reload: reloadSourceStatus } = useFetch(() => API.getSourceStatus(), []);
  const { data: customersData } = useFetch(() => API.listCustomers(), []);
  const { data: kolsData } = useFetch(() => API.listAdminKols(), []);

  const res: any = data || {};
  const summary = res.summary || {};
  const signals: any[] = res.signals || [];
  const failedOrders: any[] = res.failed_orders || [];
  const auditLogs: any[] = res.audit_logs || [];
  const customers: any[] = customersData || [];
  const kols: any[] = kolsData || [];
  const sourceStatus: any = sourceStatusData || {};
  const sourceHealthy = !!sourceStatus.healthy;
  const sourceConfigured = sourceStatus.configured !== false;

  useEffect(() => {
    const timer = window.setInterval(() => reloadSourceStatus(), 30000);
    return () => window.clearInterval(timer);
  }, [reloadSourceStatus]);

  return (
    <div className="space-y-5 md:space-y-6">
      <div className="flex flex-col lg:flex-row lg:items-center justify-between gap-3">
        <div>
          <h1 className="text-xl font-bold gradient-text flex items-center gap-2">
            <FileSearch size={20} />
            跟单诊断
          </h1>
          <p className="text-sm text-text-tertiary mt-1">集中查看为什么没跟单: 信号拒绝、过滤、订单失败和审计日志</p>
        </div>
        <Button variant="ghost" onClick={() => { reload(); reloadSourceStatus(); }}>
          <RefreshCw size={15} /> 刷新
        </Button>
      </div>

      <Card>
        <CardTitle
          action={
            <Badge tone={!sourceConfigured ? "warn" : sourceHealthy ? "profit" : "loss"}>
              {!sourceConfigured ? "未配置" : sourceHealthy ? "转发源正常" : "转发源异常"}
            </Badge>
          }
        >
          转发源连接状态
        </CardTitle>
        <div className="grid grid-cols-2 lg:grid-cols-4 gap-3 text-xs">
          <div className="glass-soft p-3">
            <div className="text-text-muted mb-1">连接状态</div>
            <div className={sourceHealthy ? "text-emerald font-bold" : "text-rose font-bold"}>
              {!sourceConfigured ? "未配置 Token" : sourceHealthy ? "已连接并有心跳" : "未连接或心跳异常"}
            </div>
          </div>
          <div className="glass-soft p-3">
            <div className="text-text-muted mb-1">最后心跳</div>
            <div className="text-text-secondary font-mono">{fmtTime(sourceStatus.last_heartbeat_ack_at)}</div>
          </div>
          <div className="glass-soft p-3">
            <div className="text-text-muted mb-1">最后收到消息</div>
            <div className="text-text-secondary font-mono">{fmtTime(sourceStatus.last_message_at)}</div>
          </div>
          <div className="glass-soft p-3">
            <div className="text-text-muted mb-1">最后 KOL 消息</div>
            <div className="text-text-secondary">
              {sourceStatus.last_kol_name || "暂无"} · {fmtTime(sourceStatus.last_kol_message_at)}
            </div>
          </div>
          <div className="glass-soft p-3">
            <div className="text-text-muted mb-1">启用 KOL</div>
            <div className="text-text-secondary font-mono">{sourceStatus.enabled_kol_count ?? "—"} 个</div>
          </div>
          <div className="glass-soft p-3">
            <div className="text-text-muted mb-1">最近入库信号</div>
            <div className="text-text-secondary font-mono">{fmtTime(sourceStatus.last_signal_at)}</div>
          </div>
          <div className="glass-soft p-3">
            <div className="text-text-muted mb-1">重连次数</div>
            <div className="text-text-secondary font-mono">{sourceStatus.reconnect_count ?? 0}</div>
          </div>
          <div className="glass-soft p-3">
            <div className="text-text-muted mb-1">异常原因</div>
            <div className={sourceStatus.last_error ? "text-rose break-words" : "text-text-secondary"}>
              {sourceStatus.last_error || "无异常"}
            </div>
          </div>
        </div>
      </Card>

      <Card>
        <div className="grid grid-cols-2 md:grid-cols-6 gap-3">
          <div>
            <div className="label">时间范围</div>
            <Input type="number" min={1} max={720} value={hours} onChange={(e) => setHours(e.target.value)} />
          </div>
          <div>
            <div className="label">信号状态</div>
            <Select value={signalStatus} onChange={(e) => setSignalStatus(e.target.value)}>
              <option value="">全部</option>
              <option value="ordered">已下单</option>
              <option value="rejected">已拒绝</option>
              <option value="filtered">已过滤</option>
              <option value="ignored">已忽略</option>
              <option value="corrected">已纠错</option>
              <option value="received">已接收</option>
            </Select>
          </div>
          <div>
            <div className="label">订单状态</div>
            <Select value={orderStatus} onChange={(e) => setOrderStatus(e.target.value)}>
              <option value="">全部</option>
              <option value="failed">失败</option>
              <option value="pending">挂单中</option>
              <option value="filled">已成交</option>
              <option value="partial">部分成交</option>
              <option value="cancelled">已撤</option>
              <option value="deleted">已删</option>
            </Select>
          </div>
          <div>
            <div className="label">客户</div>
            <Select value={customerId} onChange={(e) => setCustomerId(e.target.value)}>
              <option value="">全部客户</option>
              {customers.map((c) => (
                <option key={c.id} value={c.id}>{c.display_name || c.username}</option>
              ))}
            </Select>
          </div>
          <div>
            <div className="label">KOL</div>
            <Select value={kolId} onChange={(e) => setKolId(e.target.value)}>
              <option value="">全部 KOL</option>
              {kols.map((k) => (
                <option key={k.id} value={k.id}>{k.name}</option>
              ))}
            </Select>
          </div>
          <div className="flex items-end">
            <div className="text-xs text-text-tertiary leading-relaxed">
              当前回看最近 <span className="text-text-secondary font-mono">{hours || 24}</span> 小时
            </div>
          </div>
        </div>
      </Card>

      <div className="grid grid-cols-2 lg:grid-cols-4 gap-3">
        <MetricCard label="信号数" value={summary.signals || 0} sub="进入诊断范围" icon={Radio} />
        <MetricCard label="已下单信号" value={summary.ordered_signals || 0} tone="profit" sub="至少进入下单流程" icon={CheckCircle2} />
        <MetricCard label="拒绝/过滤" value={(summary.rejected_signals || 0) + (summary.filtered_signals || 0)} tone="loss" sub="重点看原因字段" icon={XCircle} />
        <MetricCard label="失败订单" value={summary.failed_orders || 0} tone="gold" sub="交易所或风控失败" icon={AlertTriangle} />
      </div>

      <div className="flex gap-2 flex-wrap">
        {[
          { key: "signals", label: `信号诊断 ${signals.length}` },
          { key: "orders", label: `失败订单 ${failedOrders.length}` },
          { key: "audit", label: `审计日志 ${auditLogs.length}` },
        ].map((item) => (
          <button
            key={item.key}
            onClick={() => setTab(item.key as any)}
            className={`px-3 py-2 rounded-xl text-sm font-semibold border transition-all ${
              tab === item.key
                ? "bg-emerald/[0.09] text-emerald border-emerald-border"
                : "glass-soft text-text-tertiary border-border/60 hover:text-text"
            }`}
          >
            {item.label}
          </button>
        ))}
      </div>

      {tab === "signals" && (
        <Card>
          <CardTitle>信号处理链路</CardTitle>
          {signals.length === 0 ? (
            <Empty text="暂无符合条件的信号" />
          ) : (
            <div className="space-y-3">
              {signals.map((sig) => (
                <div key={sig.id} className="glass-soft p-4 border-l-2 border-l-border-soft">
                  <div className="flex items-center gap-2 flex-wrap">
                    <span className="text-sm font-semibold text-text">{sig.kol_name || "未知 KOL"}</span>
                    {sig.symbol && <Badge tone="accent" className="text-[10px]">{sig.symbol}</Badge>}
                    {sig.side && <Badge tone={sig.side === "long" ? "profit" : "loss"} className="text-[10px]">{sideLabel(sig.side)}</Badge>}
                    <Badge tone={signalTone[sig.status] || "default"} className="text-[10px]">{signalStatusLabel(sig.status)}</Badge>
                    {sig.corrected && <Badge tone="warn" className="text-[10px]">已纠错</Badge>}
                    <span className="text-xs text-text-muted ml-auto">{fmtTime(sig.received_at)}</span>
                  </div>
                  <div className="mt-2 text-xs text-text-tertiary font-mono break-all line-clamp-2">
                    {sig.raw_text || "(无原文)"}
                  </div>
                  <ReasonBox text={sig.reason} />
                  {sig.orders?.length > 0 && (
                    <div className="mt-3 space-y-2">
                      <div className="text-xs font-semibold text-text-secondary flex items-center gap-1">
                        <ShoppingCart size={13} /> 关联订单 {sig.order_count} 个
                      </div>
                      {sig.orders.slice(0, 8).map((order: any) => <OrderMini key={order.id} order={order} />)}
                    </div>
                  )}
                </div>
              ))}
            </div>
          )}
        </Card>
      )}

      {tab === "orders" && (
        <Card>
          <CardTitle>失败/异常订单</CardTitle>
          {failedOrders.length === 0 ? (
            <Empty text="暂无失败订单" />
          ) : (
            <div className="space-y-2">
              {failedOrders.map((order) => <OrderMini key={order.id} order={order} />)}
            </div>
          )}
        </Card>
      )}

      {tab === "audit" && (
        <Card>
          <CardTitle>系统审计日志</CardTitle>
          {auditLogs.length === 0 ? (
            <Empty text="暂无审计日志" />
          ) : (
            <div className="space-y-2">
              {auditLogs.map((log) => (
                <div key={log.id} className="glass-soft p-3 text-xs">
                  <div className="flex items-center gap-2 flex-wrap">
                    <Badge tone="default" className="text-[10px]">{log.action}</Badge>
                    <span className="text-text-secondary">{log.target || "system"}</span>
                    <span className="text-text-muted ml-auto flex items-center gap-1">
                      <Clock size={12} /> {fmtTime(log.created_at)}
                    </span>
                  </div>
                  {log.detail && <div className="mt-1 text-text-tertiary whitespace-pre-wrap break-words">{log.detail}</div>}
                </div>
              ))}
            </div>
          )}
        </Card>
      )}
    </div>
  );
}
