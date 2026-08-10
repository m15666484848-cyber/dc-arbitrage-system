import { useMemo } from "react";
import { API } from "@/api/client";
import { useFetch } from "@/lib/useFetch";
import { useAccountFilterStore } from "@/stores/accountFilter";

export function accountName(acc: any) {
  if (!acc) return "";
  const label = (acc.label || "").trim();
  const exchangeKey = String(acc.exchange || "").toLowerCase();
  const exchangeName =
    {
      binance: "币安",
      okx: "OKX",
      bybit: "Bybit",
    }[exchangeKey] || String(acc.exchange || "未知交易所").toUpperCase();
  const mode = String(acc.account_mode || (acc.testnet ? "testnet" : "live")).toLowerCase();
  const modeName =
    {
      live: "实盘",
      testnet: "测试网",
      demo: "模拟盘",
    }[mode] || mode;
  return `${exchangeName} · ${modeName}${label ? ` · ${label}` : ""} #${acc.id}`;
}

export default function AccountFilter({ className = "" }: { className?: string }) {
  const { accountId, setAccountId } = useAccountFilterStore();
  const { data } = useFetch(() => API.listExchangeAccounts(), []);
  const accounts: any[] = Array.isArray(data) ? data : [];

  const currentLabel = useMemo(() => {
    if (!accountId) return "全部API";
    return accountName(accounts.find((a) => a.id === accountId)) || `API #${accountId}`;
  }, [accounts, accountId]);

  // 仅多API用户显示
  if (accounts.length <= 1) return null;

  return (
    <div className={`flex items-center gap-1.5 ${className}`}>
      <span className="text-xs text-text-tertiary whitespace-nowrap hidden lg:inline">API账户</span>
      <select
        value={accountId ? String(accountId) : ""}
        onChange={(e) => setAccountId(e.target.value ? Number(e.target.value) : null)}
        title={currentLabel}
        className="px-2.5 py-1.5 rounded-lg bg-bg-card/60 border border-border/60 text-xs text-text-secondary focus:outline-none focus:border-accent/40 cursor-pointer min-w-[140px] max-w-[200px]"
      >
        <option value="">全部API</option>
        {accounts.map((acc) => (
          <option key={acc.id} value={acc.id}>
            {accountName(acc)}
          </option>
        ))}
      </select>
    </div>
  );
}
