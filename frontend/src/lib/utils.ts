import { type ClassValue, clsx } from "clsx";
import { twMerge } from "tailwind-merge";

export function cn(...inputs: ClassValue[]) {
  return twMerge(clsx(inputs));
}

export function fmtMoney(value: number | string | undefined, digits = 2): string {
  const n = typeof value === "string" ? parseFloat(value) : typeof value === "number" ? value : 0;
  if (!isFinite(n)) return "—";
  return n.toLocaleString("en-US", { minimumFractionDigits: digits, maximumFractionDigits: digits });
}

export function fmtPct(value: number | string | undefined): string {
  const n = typeof value === "string" ? parseFloat(value) : typeof value === "number" ? value : 0;
  if (!isFinite(n)) return "—";
  return `${n.toFixed(2)}%`;
}

export function fmtTime(value: string | number | undefined): string {
  if (!value) return "—";
  const d = new Date(value);
  // S16v2: try/catch 捕不到 Invalid Date,用 isNaN 检测
  if (isNaN(d.getTime())) return String(value);
  return d.toLocaleString("zh-CN", { hour12: false });
}

export const SIGNAL_STATUS_LABEL: Record<string, string> = {
  received: "已接收",
  parsed: "已解析",
  filtered: "已过滤",
  corrected: "已纠错",
  ordered: "已下单",
  rejected: "已拒绝",
  ignored: "已忽略",
};

export const ORDER_STATUS_LABEL: Record<string, string> = {
  pending: "挂单中",
  filled: "已成交",
  partial: "部分成交",
  cancelled: "已撤单",
  canceled: "已撤单",
  deleted: "已删除",
  failed: "下单失败",
};

export const SIDE_LABEL: Record<string, string> = {
  long: "做多",
  short: "做空",
  buy: "买入",
  sell: "卖出",
};

export function signalStatusLabel(status?: string): string {
  if (!status) return "未知状态";
  return SIGNAL_STATUS_LABEL[status] || status;
}

export function orderStatusLabel(status?: string): string {
  if (!status) return "未知状态";
  return ORDER_STATUS_LABEL[status] || status;
}

export function sideLabel(side?: string): string {
  if (!side) return "";
  return SIDE_LABEL[side] || side;
}

export function pnlColor(value: number | string | undefined): string {
  const n = typeof value === "string" ? parseFloat(value) : typeof value === "number" ? value : 0;
  if (n > 0) return "text-profit";
  if (n < 0) return "text-loss";
  return "text-slate-300";
}
