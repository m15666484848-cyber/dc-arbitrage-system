import axios, { AxiosError } from "axios";
import { useAuthStore } from "@/stores/auth";
const client = axios.create({ baseURL: "/api", timeout: 15000, withCredentials: true });
client.interceptors.request.use((cfg) => {
  const token = useAuthStore.getState().token;
  if (token) cfg.headers.Authorization = `Bearer ${token}`;
  return cfg;
});
// S9修复: 401时尝试refresh token,避免用户频繁被登出
let isRefreshing = false;
let refreshPromise: Promise<any> | null = null;

async function doRefresh(): Promise<any> {
  if (isRefreshing && refreshPromise) return refreshPromise;
  isRefreshing = true;
  refreshPromise = (async () => {
    const res = await axios.post("/api/auth/refresh", {}, { withCredentials: true });
    return res.data?.data ?? res.data;
  })();
  try {
    const result = await refreshPromise;
    isRefreshing = false;
    refreshPromise = null;
    return result;
  } catch (e) {
    isRefreshing = false;
    refreshPromise = null;
    throw e;
  }
}

client.interceptors.response.use(
  (r) => r,
  async (err: AxiosError) => {
    const originalRequest = err.config as any;
    // 排除auth端点本身的401(避免循环)
    const isAuthEndpoint = originalRequest?.url?.startsWith("/auth/");
    if (err.response?.status === 401 && !originalRequest._retry && !isAuthEndpoint) {
      originalRequest._retry = true;
      try {
        const res = await doRefresh();
        useAuthStore.getState().setAuth(res.access_token, {
          id: res.user_id,
          username: res.username,
          role: res.role,
          display_name: res.display_name,
          authorization: res.authorization,
          show_signal_summary: res.show_signal_summary,
          emergency_stop: res.emergency_stop,
        });
        originalRequest.headers.Authorization = `Bearer ${res.access_token}`;
        return client.request(originalRequest);
      } catch {
        useAuthStore.getState().logout();
        if (window.location.pathname !== "/login") {
          window.location.href = "/login";
        }
        return Promise.reject(err);
      }
    }
    if (err.response?.status === 401) {
      useAuthStore.getState().logout();
      if (window.location.pathname !== "/login") {
        window.location.href = "/login";
      }
    }
    return Promise.reject(err);
  }
);
export async function api<T = any>(method: string, url: string, data?: any, params?: any): Promise<T> {
  const res = await client.request({ method, url, data, params });
  return res.data?.data ?? res.data;
}
// API 基础路径(用于需要拼接完整 URL 的场景, 如文件下载 / window.open)
export const API_BASE = "/api";
export const API = {
  // auth
  login: (username: string, password: string) => api("post", "/auth/login", { username, password }),
  refreshToken: () => api("post", "/auth/refresh"),
  logout: () => api("post", "/auth/logout"),
  register: (username: string, password: string, display_name?: string, invite_code?: string) =>
    api("post", "/auth/register", { username, password, display_name, invite_code }),
  me: () => api("get", "/auth/me"),
  toggleEmergencyStop: () => api("post", "/emergency-stop"),
  getPublicSourceStatus: () => api("get", "/source-status"),
  // admin
  listUsers: () => api("get", "/admin/users"),
  createUser: (data: any) => api("post", "/admin/users", data),
  listCustomers: () => api("get", "/admin/customers"),
  createCustomer: (data: any) => api("post", "/admin/customers", data),
  updateCustomer: (id: number, data: any) => api("put", `/admin/customers/${id}`, data),
  deleteCustomer: (id: number, confirm_username: string) =>
    api("delete", `/admin/customers/${id}`, undefined, { confirm_username }),
  // 邀请码 / 邀请链接
  getInviteLink: (cid: number) => api("get", `/admin/customers/${cid}/invite-link`),
  loginAsCustomer: (cid: number) => api("post", `/admin/customers/${cid}/login-as`),
  // 设置客户类型(normal=普通客户, internal=内部用户)
  setCustomerType: (cid: number, customer_type: string) =>
    api("put", `/admin/customers/${cid}/type`, { customer_type }),
  // 利润统计
  getProfitStats: (params: { start_date: string; end_date: string; customer_type?: string }) =>
    api("get", "/admin/profit-stats", null, params),
  exportProfitStats: (params: { start_date: string; end_date: string; customer_type?: string; profit_percentage?: number }) =>
    `${API_BASE}/admin/profit-stats/export?start_date=${encodeURIComponent(params.start_date)}&end_date=${encodeURIComponent(params.end_date)}${params.customer_type ? `&customer_type=${encodeURIComponent(params.customer_type)}` : ""}${params.profit_percentage ? `&profit_percentage=${params.profit_percentage}` : ""}`,
  listAuthorizations: (cid: number) => api("get", `/admin/authorizations/${cid}`),
  grantAuth: (data: any) => api("post", "/admin/authorizations", data),
  updateAuth: (id: number, data: any) => api("put", `/admin/authorizations/${id}`, data),
  revokeAuth: (id: number) => api("delete", `/admin/authorizations/${id}`),
  listAdminKols: () => api("get", "/admin/kols"),
  createKol: (data: any) => api("post", "/admin/kols", data),
  updateKol: (id: number, data: any) => api("put", `/admin/kols/${id}`, data),
  deleteKol: (id: number) => api("delete", `/admin/kols/${id}`),
  listDiscordAccounts: () => api("get", "/admin/discord-accounts"),
  createDiscordAccount: (data: any) => api("post", "/admin/discord-accounts", data),
  updateDiscordAccount: (id: number, data: any) => api("put", `/admin/discord-accounts/${id}`, data),
  deleteDiscordAccount: (id: number) => api("delete", `/admin/discord-accounts/${id}`),
  getFollowDiagnosis: (params: {
    hours?: number;
    limit?: number;
    signal_status?: string;
    order_status?: string;
    customer_id?: number;
    kol_id?: number;
  }) => api("get", "/admin/diagnosis", null, params),
  getSourceStatus: () => api("get", "/admin/source-status"),
  // 系统配置(LLM + Discord)
  getSystemConfig: () => api("get", "/admin/system-config"),
  updateSystemConfig: (data: any) => api("put", "/admin/system-config", data),
  testLlm: (llm_type: "text" | "vision" = "text") =>
    api("post", `/admin/system-config/test-llm?llm_type=${llm_type}`),
  simulateKolSignal: (data: any) => api("post", "/admin/simulate-kol-signal", data),
  // trading
  listKols: () => api("get", "/kols"),
  setFollows: (kol_settings: { kol_id: number; strategy_id: number | null; notional_usdt: number | null }[]) =>
    api("post", "/kols/follow", { kol_settings: kol_settings }),
  resumeKolFollow: (kol_id: number) => api("post", `/kols/${kol_id}/resume`),
  listPositions: (exchange_account_id?: number | null, cid?: number) => api("get", "/positions", undefined, { exchange_account_id: exchange_account_id || undefined, customer_id: cid }),
  closePosition: (position_id: number, qty?: number) =>
    api("post", "/positions/close", { position_id, qty }),
  updateStop: (data: any) => api("put", "/positions/stop", data),
  listOrders: (exchange_account_id?: number | null, cid?: number, status?: string) => api("get", "/orders", undefined, { exchange_account_id: exchange_account_id || undefined, customer_id: cid, status }),
  deleteOrder: (order_id: number) => api("post", "/orders/delete", { order_id }),
  manualOrder: (data: any) => api("post", "/orders/manual", data),
  listTrades: (exchange_account_id?: number | null, cid?: number) => api("get", "/trades", undefined, { exchange_account_id: exchange_account_id || undefined, customer_id: cid }),
  dailyStats: (month: string, exchange_account_id?: number | null, cid?: number) => api("get", "/daily-stats", undefined, { month, exchange_account_id: exchange_account_id || undefined, customer_id: cid }),
  listPendingOrders: (status: string = "pending", exchange_account_id?: number | null) => api("get", "/pending-orders", undefined, { status, exchange_account_id: exchange_account_id || undefined }),
  cancelPendingOrder: (pending_id: number, reason: string = "用户手动取消") =>
    api("post", "/pending-orders/cancel", { pending_id, reason }),
  // symbol multipliers (customer)
  getSymbolMultipliers: () => api("get", "/symbol-multipliers"),
  setSymbolMultipliers: (data: { config_id: number; multiplier: number }[]) =>
    api("post", "/symbol-multipliers", data),
  resetSymbolMultiplier: (config_id: number) => api("delete", `/symbol-multipliers/${config_id}`),
  // symbol categories CRUD (customer)
  createSymbolCategory: (data: { name: string; symbols?: string; multiplier?: number; note?: string }) =>
    api("post", "/symbol-categories", data),
  updateSymbolCategory: (id: number, data: { name?: string; symbols?: string; multiplier?: number; note?: string }) =>
    api("put", `/symbol-categories/${id}`, data),
  deleteSymbolCategory: (id: number) => api("delete", `/symbol-categories/${id}`),
  // custom symbols (customer)
  getCustomSymbols: () => api("get", "/custom-symbols"),
  addCustomSymbol: (data: { symbol: string; multiplier: number }) =>
    api("post", "/custom-symbols", data),
  updateCustomSymbol: (id: number, data: { multiplier: number }) =>
    api("put", `/custom-symbols/${id}`, data),
  deleteCustomSymbol: (id: number) => api("delete", `/custom-symbols/${id}`),
  // strategy
  listStrategies: () => api("get", "/strategies"),
  createStrategy: (data: any) => api("post", "/strategies", data),
  updateStrategy: (id: number, data: any) => api("put", `/strategies/${id}`, data),
  deleteStrategy: (id: number) => api("delete", `/strategies/${id}`),
  // analytics
  dashboard: (exchange_account_id?: number | null) => api("get", "/dashboard", undefined, { exchange_account_id: exchange_account_id || undefined }),
  kolRanking: (days = 30) => api("get", `/kol-ranking?days=${days}`),
  equityCurve: (days = 30, exchange_account_id?: number | null) =>
    api("get", "/equity-curve", undefined, { days, exchange_account_id: exchange_account_id || undefined }),
  getEquityHistory: (days = 90, exchange_account_id?: number | null) =>
    api("get", "/equity-curve", undefined, { days, exchange_account_id: exchange_account_id || undefined }),
  listSignals: (page = 1, page_size = 50, kol_id?: number, status?: string) =>
    api("get", `/signals?page=${page}&page_size=${page_size}${kol_id ? `&kol_id=${kol_id}` : ""}${status ? `&status=${status}` : ""}`),
  injectSignal: (data: any) => api("post", "/signals/inject", data),
  // settings
  listExchangeAccounts: () => api("get", "/exchange-accounts"),
  addExchangeAccount: (data: any) => api("post", "/exchange-accounts", data),
  deleteExchangeAccount: (id: number) => api("delete", `/exchange-accounts/${id}`),
  testExchangeAccount: (id: number) => api("post", `/exchange-accounts/${id}/test`),
  setDefaultExchangeAccount: (id: number) => api("post", `/exchange-accounts/${id}/default`),
  updateExchangeAccountFollow: (id: number, data: any) => api("put", `/exchange-accounts/${id}/follow`, data),
  getExchangeAccountBalance: (id: number) => api("get", `/exchange-accounts/${id}/balance`),
  getExchangeBalanceSummary: () => api("get", "/exchange-account-balances/summary"),
  getExchangeRiskOverview: () => api("get", "/exchange-account-risk-overview"),
  getRiskConfig: () => api("get", "/risk-config"),
  upsertRiskConfig: (data: any) => api("put", "/risk-config", data),
  listAlerts: () => api("get", "/alerts"),
  addAlert: (data: any) => api("post", "/alerts", data),
  updateAlert: (id: number, data: any) => api("put", `/alerts/${id}`, data),
  toggleAlert: (id: number) => api("patch", `/alerts/${id}/toggle`),
  deleteAlert: (id: number) => api("delete", `/alerts/${id}`),
  listAlertLogs: () => api("get", "/alert-logs"),
  // symbol notional config (admin)
  listSymbolNotional: () => api("get", "/admin/symbol-notional"),
  createSymbolNotional: (data: any) => api("post", "/admin/symbol-notional", data),
  updateSymbolNotional: (id: number, data: any) => api("put", `/admin/symbol-notional/${id}`, data),
  deleteSymbolNotional: (id: number) => api("delete", `/admin/symbol-notional/${id}`),
  // 客户告警管理(管理员)
  listCustomerAlerts: (cid: number) => api("get", `/admin/customers/${cid}/alerts`),
  createCustomerAlert: (cid: number, data: any) => api("post", `/admin/customers/${cid}/alerts`, data),
  updateCustomerAlert: (cid: number, aid: number, data: any) => api("put", `/admin/customers/${cid}/alerts/${aid}`, data),
  deleteCustomerAlert: (cid: number, aid: number) => api("delete", `/admin/customers/${cid}/alerts/${aid}`),
  // 客户邀请系统
  getMyInvitees: () => api("get", "/auth/my-invitees"),
  // password
  changePassword: (old_password: string, new_password: string) => api("put", "/auth/change-password", { old_password, new_password }),
  resetCustomerPassword: (cid: number, new_password: string) => api("post", `/admin/customers/${cid}/reset-password`, { new_password }),
  // 清除测试数据(信号/订单/持仓/交易/日志),保留配置
  // customer_id 为 null 时清除所有数据,指定时只清除该客户数据
  resetData: (confirm_text: string, reset_strategy_state: boolean = true, customer_id: number | null = null) =>
    api("post", "/admin/reset-data", { confirm_text, reset_strategy_state, customer_id }),
};
export default client;
