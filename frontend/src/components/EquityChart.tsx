import { useMemo } from "react";
import {
  Area,
  AreaChart,
  CartesianGrid,
  ResponsiveContainer,
  Tooltip,
  XAxis,
  YAxis,
} from "recharts";

interface Point {
  snapshot_at: string;
  equity: number;
  balance: number;
  unrealized_pnl?: number;
}

export function EquityChart({ data, height = 300 }: { data: Point[]; height?: number }) {
  const chartData = useMemo(
    () =>
      (data || []).map((p) => ({
        time: new Date(p.snapshot_at).toLocaleString("zh-CN", {
          hour12: false,
          month: "2-digit",
          day: "2-digit",
          hour: "2-digit",
          minute: "2-digit",
        }),
        equity: p.equity,
        balance: p.balance,
      })),
    [data]
  );

  if (!chartData.length) {
    return (
      <div className="flex items-center justify-center text-slate-500 text-sm" style={{ height }}>
        暂无净值数据(每 5 分钟采集一次)
      </div>
    );
  }

  return (
    <ResponsiveContainer width="100%" height={height}>
      <AreaChart data={chartData} margin={{ top: 10, right: 10, left: 0, bottom: 0 }}>
        <defs>
          <linearGradient id="eq" x1="0" y1="0" x2="0" y2="1">
            <stop offset="5%" stopColor="#38bdf8" stopOpacity={0.35} />
            <stop offset="95%" stopColor="#38bdf8" stopOpacity={0} />
          </linearGradient>
          <linearGradient id="bal" x1="0" y1="0" x2="0" y2="1">
            <stop offset="5%" stopColor="#22c55e" stopOpacity={0.25} />
            <stop offset="95%" stopColor="#22c55e" stopOpacity={0} />
          </linearGradient>
        </defs>
        <CartesianGrid strokeDasharray="3 3" stroke="#1e2738" vertical={false} />
        <XAxis
          dataKey="time"
          stroke="#64748b"
          fontSize={11}
          tickLine={false}
          axisLine={false}
          minTickGap={40}
          tick={{ fill: "#64748b" }}
        />
        <YAxis
          stroke="#64748b"
          fontSize={11}
          tickLine={false}
          axisLine={false}
          domain={["auto", "auto"]}
          tick={{ fill: "#64748b" }}
          tickFormatter={(v: number) =>
            v >= 10000 ? `${(v / 10000).toFixed(1)}w` : v.toLocaleString()
          }
        />
        <Tooltip
          contentStyle={{
            background: "#0f1420",
            border: "1px solid #2a3650",
            borderRadius: 12,
            color: "#e2e8f0",
            fontSize: 12,
            boxShadow: "0 8px 32px -8px rgba(0,0,0,0.6)",
          }}
          labelStyle={{ color: "#94a3b8" }}
          formatter={(value: number, name: string) => [
            value.toLocaleString("en-US", { minimumFractionDigits: 2, maximumFractionDigits: 2 }),
            name,
          ]}
        />
        <Area
          type="monotone"
          dataKey="equity"
          stroke="#38bdf8"
          strokeWidth={2.5}
          fill="url(#eq)"
          name="账户净值"
          dot={false}
          activeDot={{ r: 4, stroke: "#0f1420", strokeWidth: 2, fill: "#38bdf8" }}
        />
        <Area
          type="monotone"
          dataKey="balance"
          stroke="#22c55e"
          strokeWidth={1.5}
          fill="url(#bal)"
          name="余额"
          dot={false}
        />
      </AreaChart>
    </ResponsiveContainer>
  );
}
