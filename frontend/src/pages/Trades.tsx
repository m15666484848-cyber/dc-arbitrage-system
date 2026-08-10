import React, { useState, useMemo, useCallback } from "react";
import { History, Trash2, Plus, TrendingUp, Clock } from "lucide-react";
import { API } from "@/api/client";
import { useFetch } from "@/lib/useFetch";
import { useToast } from "@/components/ui/Toast";
import { Card, CardTitle, Badge, Button, Empty, Input, Select, MetricCard } from "@/components/ui";
import { useAccountFilterStore } from "@/stores/accountFilter";
import { Modal } from "@/components/ui/Modal";
import { fmtMoney, fmtTime, orderStatusLabel, pnlColor, sideLabel } from "@/lib/utils";

const OrderRow = React.memo(function OrderRow({
  o,
  delId,
  onDelete,
}: {
  o: any;
  delId: number | null;
  onDelete: (id: number) => void;
}) {
  return (
    <tr className="border-b border-border/50 hover:bg-bg-hover/40">
      <td className="py-2.5 px-2 text-slate-300">{o.kol_name || "手动"}</td>
      <td className="px-2 font-mono text-slate-100">{o.symbol}</td>
      <td className="px-2"><Badge tone={o.side === "buy" ? "profit" : "loss"}>{sideLabel(o.side)}</Badge></td>
      <td className="px-2 text-slate-400">{o.type}</td>
      <td className="px-2 text-right font-mono text-slate-300">{fmtMoney(o.qty, 4)}</td>
      <td className="px-2 text-right font-mono text-slate-300">{o.price ? fmtMoney(o.price, 4) : "市价"}</td>
      <td className="px-2 text-center">
        <Badge tone={o.status === "filled" ? "profit" : o.status === "pending" ? "warn" : "default"}>{orderStatusLabel(o.status)}</Badge>
      </td>
      <td className="px-2 text-xs text-slate-500">{fmtTime(o.created_at)}</td>
      <td className="px-2 text-center">
        {o.status === "pending" && (
          <Button variant="danger" className="px-2 py-1 text-xs" disabled={delId === o.id} onClick={() => onDelete(o.id)}>
            <Trash2 size={12} /> {delId === o.id ? "..." : "删除"}
          </Button>
        )}
      </td>
    </tr>
  );
});

const OrderCard = React.memo(function OrderCard({
  o,
  delId,
  onDelete,
}: {
  o: any;
  delId: number | null;
  onDelete: (id: number) => void;
}) {
  return (
    <div className="glass-soft p-3">
      <div className="flex items-start justify-between gap-2 mb-2">
        <div className="min-w-0 flex-1">
          <div className="flex items-center gap-1.5 flex-wrap">
            <Badge tone={o.side === "buy" ? "profit" : "loss"} className="text-[10px]">
              {sideLabel(o.side)}
            </Badge>
            <span className="font-mono text-sm font-bold text-slate-100">{o.symbol}</span>
            <Badge tone={o.status === "filled" ? "profit" : o.status === "pending" ? "warn" : "default"} className="text-[10px]">
              {orderStatusLabel(o.status)}
            </Badge>
          </div>
          <div className="text-xs text-slate-500 mt-1 truncate">{o.kol_name || "手动"} · {o.type}</div>
        </div>
        <div className="text-right shrink-0 text-xs text-slate-500">{fmtTime(o.created_at)}</div>
      </div>
      <div className="grid grid-cols-2 gap-2 text-xs">
        <div>
          <div className="text-slate-500">数量</div>
          <div className="font-mono text-slate-300">{fmtMoney(o.qty, 4)}</div>
        </div>
        <div>
          <div className="text-slate-500">价格</div>
          <div className="font-mono text-slate-300">{o.price ? fmtMoney(o.price, 4) : "市价"}</div>
        </div>
      </div>
      {o.status === "pending" && (
        <div className="mt-2.5 pt-2.5 border-t border-border/50">
          <Button
            variant="danger"
            className="w-full text-xs min-h-[36px]"
            disabled={delId === o.id}
            onClick={() => onDelete(o.id)}
          >
            <Trash2 size={14} /> {delId === o.id ? "删除中..." : "删除订单"}
          </Button>
        </div>
      )}
    </div>
  );
});

const TradeRow = React.memo(function TradeRow({ t }: { t: any }) {
  return (
    <tr className="border-b border-border/50 hover:bg-bg-hover/40">
      <td className="py-2.5 px-2 text-slate-300">{t.kol_name || "手动"}</td>
      <td className="px-2 font-mono text-slate-100">{t.symbol}</td>
      <td className="px-2"><Badge tone={t.side === "buy" ? "profit" : "loss"}>{sideLabel(t.side)}</Badge></td>
      <td className="px-2 text-right font-mono text-slate-300">{fmtMoney(t.qty, 4)}</td>
      <td className="px-2 text-right font-mono text-slate-300">{fmtMoney(t.price, 4)}</td>
      <td className="px-2 text-center">
        {t.is_close ? (
          <Badge tone={t.tp_level > 0 ? "profit" : "default"}>
            {t.tp_level > 0 ? `TP${t.tp_level}平仓` : t.tp_level === -1 ? "手动平仓" : "平仓"}
          </Badge>
        ) : (
          <Badge>开仓</Badge>
        )}
      </td>
      <td className={`px-2 text-right font-mono font-semibold ${pnlColor(t.realized_pnl)}`}>
        {t.is_close ? fmtMoney(t.realized_pnl) : "—"}
      </td>
      <td className="px-2 text-xs text-slate-500">{fmtTime(t.executed_at)}</td>
    </tr>
  );
});

const TradeCard = React.memo(function TradeCard({ t }: { t: any }) {
  return (
    <div className="glass-soft p-3">
      <div className="flex items-start justify-between gap-2 mb-2">
        <div className="min-w-0 flex-1">
          <div className="flex items-center gap-1.5 flex-wrap">
            <Badge tone={t.side === "buy" ? "profit" : "loss"} className="text-[10px]">
              {sideLabel(t.side)}
            </Badge>
            <span className="font-mono text-sm font-bold text-slate-100">{t.symbol}</span>
            {t.is_close ? (
              <Badge tone={t.tp_level > 0 ? "profit" : "default"} className="text-[10px]">
                {t.tp_level > 0 ? `TP${t.tp_level}` : t.tp_level === -1 ? "手动平" : "平仓"}
              </Badge>
            ) : (
              <Badge className="text-[10px]">开仓</Badge>
            )}
          </div>
          <div className="text-xs text-slate-500 mt-1 truncate">{t.kol_name || "手动"}</div>
        </div>
        {t.is_close && (
          <div className={`text-sm font-bold font-mono shrink-0 ${pnlColor(t.realized_pnl)}`}>
            {fmtMoney(t.realized_pnl)}
          </div>
        )}
      </div>
      <div className="grid grid-cols-3 gap-2 text-xs">
        <div>
          <div className="text-slate-500">数量</div>
          <div className="font-mono text-slate-300">{fmtMoney(t.qty, 4)}</div>
        </div>
        <div>
          <div className="text-slate-500">成交价</div>
          <div className="font-mono text-slate-300">{fmtMoney(t.price, 4)}</div>
        </div>
        <div>
          <div className="text-slate-500">时间</div>
          <div className="text-slate-400 truncate">{fmtTime(t.executed_at)}</div>
        </div>
      </div>
    </div>
  );
});

export default function TradesPage() {
  const { accountId } = useAccountFilterStore();
  const { data: tradesData, reload: reloadTrades } = useFetch(() => API.listTrades(accountId), [accountId]);
  const { data: ordersData, reload: reloadOrders } = useFetch(() => API.listOrders(accountId), [accountId]);
  const { push } = useToast();
  const [delId, setDelId] = useState<number | null>(null);
  const [manual, setManual] = useState(false);
  const [view, setView] = useState<"orders" | "trades" | "timeline">("orders");
  const [form, setForm] = useState({
    exchange: "okx", symbol: "", side: "buy", type: "market", qty: 50, price: "", leverage: 1,
    take_profits: "", stop_loss: "",
  });

  const trades: any[] = tradesData || [];
  const orders: any[] = useMemo(
    () => (ordersData || []).filter((o: any) => o.status !== "deleted"),
    [ordersData]
  );
  const totalRealized = useMemo(
    () => trades.reduce((sum: number, t: any) => sum + (t.realized_pnl || 0), 0),
    [trades]
  );

  const doDelete = useCallback(async (id: number) => {
    setDelId(id);
    try {
      await API.deleteOrder(id);
      push("success", "订单已删除");
      reloadOrders();
      reloadTrades();
    } catch (e: any) {
      push("error", e?.response?.data?.message || "删除失败");
    } finally {
      setDelId(null);
    }
  }, [push, reloadOrders, reloadTrades]);

  const doManual = useCallback(async () => {
    try {
      await API.manualOrder({
        ...form,
        qty: Number(form.qty),
        price: form.price ? Number(form.price) : null,
        leverage: Number(form.leverage),
        take_profits: form.take_profits ? form.take_profits.split(/[\s,\/]+/).map(Number) : [],
        stop_loss: form.stop_loss ? Number(form.stop_loss) : null,
      });
      push("success", "手动下单成功");
      setManual(false);
      reloadOrders();
      reloadTrades();
    } catch (e: any) {
      push("error", e?.response?.data?.message || "下单失败");
    }
  }, [form, push, reloadOrders, reloadTrades]);

  const pendingCount = useMemo(
    () => orders.filter((o: any) => o.status === "pending").length,
    [orders]
  );
  const filledOrderCount = useMemo(
    () => orders.filter((o: any) => o.status === "filled").length,
    [orders]
  );
  const closeTradeCount = useMemo(
    () => trades.filter((t: any) => t.is_close).length,
    [trades]
  );
  const timeline = useMemo(() => {
    const orderItems = orders.map((o: any) => ({
      kind: "order",
      id: `order-${o.id}`,
      raw: o,
      time: o.created_at,
      ts: new Date(o.created_at || 0).getTime(),
    }));
    const tradeItems = trades.map((t: any) => ({
      kind: "trade",
      id: `trade-${t.id}`,
      raw: t,
      time: t.executed_at,
      ts: new Date(t.executed_at || 0).getTime(),
    }));
    return [...orderItems, ...tradeItems].sort((a, b) => b.ts - a.ts).slice(0, 80);
  }, [orders, trades]);
  const tabs = [
    { key: "orders", label: "订单管理", desc: "挂单、已成交、删除未成交单", count: orders.length },
    { key: "trades", label: "成交流水", desc: "真实成交与已实现盈亏", count: trades.length },
    { key: "timeline", label: "全部时间线", desc: "订单和成交按时间合并", count: timeline.length },
  ] as const;

  return (
    <div className="space-y-4 md:space-y-6">
      <div className="flex items-center justify-between gap-3">
        <div className="min-w-0">
          <h1 className="text-xl font-bold gradient-text flex items-center gap-2">交易记录</h1>
          <p className="text-sm text-slate-500 mt-1 hidden sm:block">成交流水与订单管理(含手动下单/删除未成交单)</p>
        </div>
        <Button onClick={() => setManual(true)} className="shrink-0">
          <Plus size={15} /> <span className="hidden sm:inline">手动下单</span>
          <span className="sm:hidden">下单</span>
        </Button>
      </div>

      {/* KPI 概览 */}
      {/* multi-api-account-filter */}
      <div className="flex justify-end">
      </div>

      <div className="grid grid-cols-2 lg:grid-cols-4 gap-3 md:gap-4">
        <MetricCard label="订单总数" value={orders.length} icon={History} tone="default" />
        <MetricCard label="待成交挂单" value={pendingCount} icon={Clock} tone="gold" />
        <MetricCard label="成交笔数" value={trades.length} icon={History} tone="profit" />
        <MetricCard
          label="总已实现盈亏"
          value={fmtMoney(totalRealized)}
          icon={TrendingUp}
          tone={totalRealized >= 0 ? "profit" : "loss"}
          trend={totalRealized >= 0 ? "up" : "down"}
        />
      </div>

      <div className="grid gap-3 md:grid-cols-3">
        {tabs.map((tab) => (
          <button
            key={tab.key}
            onClick={() => setView(tab.key)}
            className={`text-left rounded-xl border p-3 transition ${
              view === tab.key
                ? "border-gold/60 bg-gold/10 shadow-[0_0_18px_-8px_rgba(240,180,41,0.55)]"
                : "border-white/10 bg-white/[0.03] hover:border-border-soft"
            }`}
          >
            <div className="flex items-center justify-between gap-2">
              <span className={view === tab.key ? "font-semibold text-gold" : "font-semibold text-slate-100"}>{tab.label}</span>
              <Badge tone={view === tab.key ? "gold" : "default"}>{tab.count}</Badge>
            </div>
            <div className="mt-1 text-xs text-slate-500">{tab.desc}</div>
          </button>
        ))}
      </div>

      {view === "orders" && (
        <Card>
          <CardTitle action={
            <div className="flex gap-2">
              <Badge tone="warn">{pendingCount} 挂单</Badge>
              <Badge tone="profit">{filledOrderCount} 已成交</Badge>
            </div>
          }>
            订单管理
          </CardTitle>
          <p className="text-xs text-slate-500 mb-3">用于管理委托订单，未成交挂单可在这里删除；成交后的真实流水请切到“成交流水”。</p>
          {orders.length === 0 ? (
            <Empty text="暂无订单" />
          ) : (
            <>
              <div className="hidden md:block overflow-x-auto">
                <table className="w-full text-sm">
                  <thead>
                    <tr className="text-xs text-slate-500 border-b border-border">
                      <th className="text-left py-2.5 px-2">KOL</th>
                      <th className="text-left px-2">品种</th>
                      <th className="text-left px-2">方向</th>
                      <th className="text-left px-2">类型</th>
                      <th className="text-right px-2">数量</th>
                      <th className="text-right px-2">价格</th>
                      <th className="text-center px-2">状态</th>
                      <th className="text-left px-2">时间</th>
                      <th className="text-center px-2">操作</th>
                    </tr>
                  </thead>
                  <tbody>
                    {orders.slice(0, 80).map((o) => (
                      <OrderRow key={o.id} o={o} delId={delId} onDelete={doDelete} />
                    ))}
                  </tbody>
                </table>
              </div>

              <div className="md:hidden space-y-2.5">
                {orders.slice(0, 40).map((o) => (
                  <OrderCard key={o.id} o={o} delId={delId} onDelete={doDelete} />
                ))}
              </div>
            </>
          )}
        </Card>
      )}

      {view === "trades" && (
        <Card>
          <CardTitle action={
            <div className="flex gap-2">
              <Badge>{trades.length} 笔成交</Badge>
              <Badge tone={totalRealized >= 0 ? "profit" : "loss"}>{fmtMoney(totalRealized)}</Badge>
            </div>
          }>
            成交流水
          </CardTitle>
          <p className="text-xs text-slate-500 mb-3">这里只展示交易所实际成交记录，适合核对开仓、平仓和已实现盈亏。</p>
          {trades.length === 0 ? (
            <Empty text="暂无成交记录" />
          ) : (
            <>
              <div className="hidden md:block overflow-x-auto">
                <table className="w-full text-sm">
                  <thead>
                    <tr className="text-xs text-slate-500 border-b border-border">
                      <th className="text-left py-2.5 px-2">KOL</th>
                      <th className="text-left px-2">品种</th>
                      <th className="text-left px-2">方向</th>
                      <th className="text-right px-2">数量</th>
                      <th className="text-right px-2">成交价</th>
                      <th className="text-center px-2">类型</th>
                      <th className="text-right px-2">已实现盈亏</th>
                      <th className="text-left px-2">成交时间</th>
                    </tr>
                  </thead>
                  <tbody>
                    {trades.map((t) => (
                      <TradeRow key={t.id} t={t} />
                    ))}
                  </tbody>
                </table>
              </div>

              <div className="md:hidden space-y-2.5">
                {trades.slice(0, 50).map((t) => (
                  <TradeCard key={t.id} t={t} />
                ))}
              </div>
            </>
          )}
        </Card>
      )}

      {view === "timeline" && (
        <Card>
          <CardTitle action={<Badge>{timeline.length} 条</Badge>}>全部时间线</CardTitle>
          <p className="text-xs text-slate-500 mb-3">按时间合并订单和成交，适合快速回看一段时间内发生了什么。</p>
          {timeline.length === 0 ? (
            <Empty text="暂无交易事件" />
          ) : (
            <div className="space-y-2.5">
              {timeline.map((item: any) => {
                if (item.kind === "order") {
                  const o = item.raw;
                  return (
                    <div key={item.id} className="glass-soft p-3 flex flex-col md:flex-row md:items-center gap-3">
                      <div className="flex-1 min-w-0">
                        <div className="flex items-center gap-2 flex-wrap">
                          <Badge tone="default">订单</Badge>
                          <span className="font-mono font-semibold text-slate-100">{o.symbol}</span>
                          <Badge tone={o.side === "buy" ? "profit" : "loss"}>{sideLabel(o.side)}</Badge>
                          <Badge tone={o.status === "filled" ? "profit" : o.status === "pending" ? "warn" : "default"}>{orderStatusLabel(o.status)}</Badge>
                        </div>
                        <div className="mt-1 text-xs text-slate-500">{o.kol_name || "手动"} · {o.type} · {fmtMoney(o.qty, 4)} @ {o.price ? fmtMoney(o.price, 4) : "市价"}</div>
                      </div>
                      <div className="flex items-center gap-3 md:justify-end">
                        <div className="text-xs text-slate-500">{fmtTime(o.created_at)}</div>
                        {o.status === "pending" && (
                          <Button variant="danger" className="px-2 py-1 text-xs" disabled={delId === o.id} onClick={() => doDelete(o.id)}>
                            <Trash2 size={12} /> 删除
                          </Button>
                        )}
                      </div>
                    </div>
                  );
                }
                const t = item.raw;
                return (
                  <div key={item.id} className="glass-soft p-3 flex flex-col md:flex-row md:items-center gap-3">
                    <div className="flex-1 min-w-0">
                      <div className="flex items-center gap-2 flex-wrap">
                        <Badge tone="accent">成交</Badge>
                        <span className="font-mono font-semibold text-slate-100">{t.symbol}</span>
                        <Badge tone={t.side === "buy" ? "profit" : "loss"}>{sideLabel(t.side)}</Badge>
                        {t.is_close ? <Badge tone="profit">平仓</Badge> : <Badge>开仓</Badge>}
                      </div>
                      <div className="mt-1 text-xs text-slate-500">{t.kol_name || "手动"} · {fmtMoney(t.qty, 4)} @ {fmtMoney(t.price, 4)}</div>
                    </div>
                    <div className="flex items-center gap-3 md:justify-end">
                      {t.is_close && <div className={`font-mono font-semibold ${pnlColor(t.realized_pnl)}`}>{fmtMoney(t.realized_pnl)}</div>}
                      <div className="text-xs text-slate-500">{fmtTime(t.executed_at)}</div>
                    </div>
                  </div>
                );
              })}
            </div>
          )}
        </Card>
      )}

      <Modal open={manual} onClose={() => setManual(false)} title="手动下单">
        <div className="space-y-4">
          <div className="grid grid-cols-2 gap-3">
            <div>
              <label className="label">交易所</label>
              <Select value={form.exchange} onChange={(e) => setForm({ ...form, exchange: e.target.value })}>
                <option value="okx">OKX</option>
                <option value="binance">Binance</option>
                <option value="bybit">Bybit</option>
              </Select>
            </div>
            <div>
              <label className="label">品种</label>
              <Input value={form.symbol} onChange={(e) => setForm({ ...form, symbol: e.target.value })} placeholder="SOL/USDT" />
            </div>
            <div>
              <label className="label">方向</label>
              <Select value={form.side} onChange={(e) => setForm({ ...form, side: e.target.value })}>
                <option value="buy">做多 buy</option>
                <option value="sell">做空 sell</option>
              </Select>
            </div>
            <div>
              <label className="label">名义价值(USDT)</label>
              <Input type="number" value={form.qty} onChange={(e) => setForm({ ...form, qty: Number(e.target.value) })} />
            </div>
            <div>
              <label className="label">杠杆</label>
              <Input type="number" value={form.leverage} onChange={(e) => setForm({ ...form, leverage: Number(e.target.value) })} />
            </div>
            <div>
              <label className="label">止盈(逗号分隔)</label>
              <Input value={form.take_profits} onChange={(e) => setForm({ ...form, take_profits: e.target.value })} placeholder="155,160,165" />
            </div>
          </div>
          <div>
            <label className="label">止损价</label>
            <Input value={form.stop_loss} onChange={(e) => setForm({ ...form, stop_loss: e.target.value })} placeholder="145" />
          </div>
          <Button className="w-full" onClick={doManual}>确认下单</Button>
        </div>
      </Modal>
    </div>
  );
}
