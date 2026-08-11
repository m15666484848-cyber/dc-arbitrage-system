import { ReactNode, useEffect, useState } from "react";
import { NavLink, useNavigate, useLocation } from "react-router-dom";
import {
  LayoutDashboard,
  Users,
  Crown,
  Radio,
  Wallet,
  History,
  SlidersHorizontal,
  Settings,
  LogOut,
  Activity,
  ShieldCheck,
  ShieldAlert,
  Menu,
  X,
  Cog,
  Gauge,
  BarChart3,
  TrendingUp,
  ChevronRight,
  Power,
  CalendarDays,
  FlaskConical,
} from "lucide-react";
import { useAuthStore } from "@/stores/auth";
import { wsClient } from "@/api/ws";
import { API } from "@/api/client";
import { cn } from "@/lib/utils";
import AccountFilter from "@/components/AccountFilter";

interface NavItem {
  to: string;
  label: string;
  icon: any;
  roles: ("admin" | "customer")[];
  permission?: "show_signal_summary";
}

const NAV: NavItem[] = [
  { to: "/dashboard", label: "仪表盘", icon: LayoutDashboard, roles: ["customer"] },
  { to: "/data-charts", label: "数据图表", icon: BarChart3, roles: ["customer"] },
  { to: "/kols", label: "KOL 排行", icon: Crown, roles: ["customer"] },
  { to: "/signals", label: "信号汇总", icon: Radio, roles: ["customer", "admin"], permission: "show_signal_summary" },
  { to: "/positions", label: "持仓管理", icon: Wallet, roles: ["customer"] },
  { to: "/trades", label: "交易记录", icon: History, roles: ["customer"] },
  { to: "/daily-stats", label: "统计日历", icon: CalendarDays, roles: ["customer"] },
  { to: "/strategies", label: "策略管理", icon: SlidersHorizontal, roles: ["customer"] },
  { to: "/settings", label: "交易设置", icon: Settings, roles: ["customer"] },
  { to: "/admin/customers", label: "客户管理", icon: Users, roles: ["admin"] },
  { to: "/admin/profit-stats", label: "利润统计", icon: TrendingUp, roles: ["admin"] },
  { to: "/admin/kols", label: "KOL 管理", icon: Crown, roles: ["admin"] },
  { to: "/admin/signals", label: "信号监控", icon: Radio, roles: ["admin"] },
  { to: "/admin/diagnosis", label: "跟单诊断", icon: ShieldAlert, roles: ["admin"] },
  { to: "/admin/simulator", label: "模拟测试", icon: FlaskConical, roles: ["admin"] },
  { to: "/admin/settings", label: "系统设置", icon: Cog, roles: ["admin"] },
  { to: "/admin/symbol-notional", label: "分类倍率", icon: Gauge, roles: ["admin"] },
];

const BOTTOM_NAV: NavItem[] = [
  { to: "/dashboard", label: "首页", icon: LayoutDashboard, roles: ["customer", "admin"] },
  { to: "/positions", label: "持仓", icon: Wallet, roles: ["customer"] },
  { to: "/signals", label: "信号", icon: Radio, roles: ["customer", "admin"], permission: "show_signal_summary" },
  { to: "/trades", label: "交易", icon: History, roles: ["customer"] },
  { to: "/daily-stats", label: "统计", icon: CalendarDays, roles: ["customer"] },
];

function Logo() {
  return (
    <div className="flex items-center gap-3">
      <div className="w-10 h-10 rounded-xl bg-gradient-to-br from-emerald to-emerald-dim flex items-center justify-center shadow-[0_0_20px_-4px_rgba(0,212,160,0.25)] shrink-0">
        <Activity size={20} className="text-bg" />
      </div>
      <div className="min-w-0">
        <div className="text-sm font-black gradient-text tracking-tight truncate">DC QUANT</div>
        <div className="text-[10px] text-text-muted tracking-widest truncate">KOL FOLLOW</div>
      </div>
    </div>
  );
}

export function Layout({ children }: { children: ReactNode }) {
  const { user, logout, setUser } = useAuthStore();
  const nav = useNavigate();
  const location = useLocation();
  const [connected, setConnected] = useState(false);
  const [drawerOpen, setDrawerOpen] = useState(false);
  const [esLoading, setEsLoading] = useState(false);
  const [sourceStatus, setSourceStatus] = useState<any>(null);

  useEffect(() => {
    wsClient.connect();
    const off = wsClient.on((event) => {
      if (event === "connected") setConnected(true);
      else if (event === "disconnected") setConnected(false);
    });
    return () => {
      off();
    };
  }, []);

  useEffect(() => {
    if (!user) return;
    let alive = true;
    const loadSourceStatus = async () => {
      try {
        const res = user.role === "admin" ? await API.getSourceStatus() : await API.getPublicSourceStatus();
        if (alive) setSourceStatus(res);
      } catch {
        if (alive) setSourceStatus({ healthy: false, configured: false, connected: false, state: "api_error", last_error: "状态接口不可用" });
      }
    };
    loadSourceStatus();
    const timer = window.setInterval(loadSourceStatus, 30000);
    return () => {
      alive = false;
      window.clearInterval(timer);
    };
  }, [user]);

  if (!user) {
    return (
      <div className="min-h-screen flex items-center justify-center bg-grid-pattern bg-[size:32px_32px]">
        <div className="text-text-tertiary text-sm">加载中...</div>
      </div>
    );
  }

  const canShowItem = (item: NavItem) =>
    !!user &&
    item.roles.includes(user.role) &&
    (user.role === "admin" || item.permission !== "show_signal_summary" || !!user.show_signal_summary);
  const items = NAV.filter(canShowItem);
  const bottomItems = BOTTOM_NAV.filter(canShowItem);
  const authorized = user?.role === "admin" || user?.authorization?.authorized;
  const sourceHealthy = !!sourceStatus?.healthy;
  const sourceConfigured = sourceStatus?.configured !== false;
  const sourceLabel = !sourceConfigured ? "转发源未配置" : sourceHealthy ? "转发源正常" : "转发源异常";
  const sourceLevel = !sourceConfigured ? "yellow" : sourceHealthy ? "green" : "red";

  const closeDrawer = () => setDrawerOpen(false);

  const handleLogout = () => {
    logout();
    nav("/login");
  };

  const handleToggleEmergencyStop = async () => {
    if (esLoading || !user) return;
    setEsLoading(true);
    try {
      const res = await API.toggleEmergencyStop();
      setUser({ ...user, emergency_stop: res.emergency_stop });
    } catch (e) {
      console.error("切换急停失败:", e);
    } finally {
      setEsLoading(false);
    }
  };

  const currentPage = items.find((i) => location.pathname.startsWith(i.to))?.label || "";

  return (
    <div className="min-h-screen flex bg-grid-pattern bg-[size:32px_32px]">
      {/* 桌面端侧边栏 */}
      <aside className="hidden md:flex w-64 shrink-0 border-r border-border/60 bg-bg/60 backdrop-blur-2xl flex-col">
        <div className="h-16 flex items-center px-5 border-b border-border/60">
          <Logo />
        </div>
        <nav className="flex-1 p-4 space-y-1.5 overflow-y-auto">
          {items.map((item) => (
            <NavLink
              key={item.to}
              to={item.to}
              className={({ isActive }) =>
                cn(
                  "group flex items-center gap-3.5 px-4 py-3 rounded-xl text-base font-semibold transition-all relative overflow-hidden",
                  isActive
                    ? "bg-emerald/[0.09] text-emerald border border-emerald-border shadow-[0_0_20px_-6px_rgba(0,212,160,0.14)]"
                    : "text-text-tertiary hover:text-text hover:bg-bg-hover/60"
                )
              }
            >
              {({ isActive }) => (
                <>
                  {isActive && (
                    <div className="absolute left-0 top-1/2 -translate-y-1/2 w-1 h-6 rounded-r-full bg-emerald shadow-[0_0_14px_rgba(0,212,160,0.55)]" />
                  )}
                  <item.icon
                    size={21}
                    className={cn(
                      "transition-colors shrink-0",
                      isActive ? "text-emerald" : "text-text-muted group-hover:text-text-secondary"
                    )}
                  />
                  <span className="truncate">{item.label}</span>
                </>
              )}
            </NavLink>
          ))}
        </nav>
        <div className="p-4 border-t border-border/60 space-y-2.5">
          <div className="flex items-center gap-2.5 px-4 py-3 rounded-xl bg-bg-card/50 border border-border/60">
            <div
              className={cn(
                "w-2 h-2 rounded-full shrink-0",
                connected ? "bg-emerald animate-pulse shadow-[0_0_8px_rgba(0,212,160,0.5)]" : "bg-text-muted"
              )}
            />
            <span className="text-[13px] text-text-tertiary truncate">
              {connected ? "实时已连接" : "连接中..."}
            </span>
          </div>
          <button
            onClick={handleLogout}
            className="w-full flex items-center gap-2.5 px-4 py-3 rounded-xl text-text-tertiary text-[15px] hover:bg-bg-hover hover:text-text transition-colors"
          >
            <LogOut size={18} />
            退出登录
          </button>
        </div>
      </aside>

      {/* 移动端抽屉 */}
      {drawerOpen && (
        <>
          <div
            className="fixed inset-0 bg-black/70 backdrop-blur-sm z-40 md:hidden"
            onClick={closeDrawer}
          />
          <aside className="fixed inset-y-0 left-0 w-72 max-w-[85%] border-r border-border/60 bg-bg-soft/95 z-50 flex flex-col animate-[slideIn_.25s_ease-out] md:hidden">
            <div className="h-16 flex items-center justify-between px-5 border-b border-border/60">
              <Logo />
              <button
                onClick={closeDrawer}
                className="w-9 h-9 rounded-lg hover:bg-bg-hover flex items-center justify-center text-text-tertiary touch-target"
                aria-label="关闭菜单"
              >
                <X size={18} />
              </button>
            </div>
            <nav className="flex-1 p-3 space-y-1 overflow-y-auto">
              {items.map((item) => (
                <NavLink
                  key={item.to}
                  to={item.to}
                  onClick={closeDrawer}
                  className={({ isActive }) =>
                    cn(
                      "group flex items-center gap-3 px-3 py-3.5 rounded-xl text-sm font-semibold transition-all relative min-h-[48px]",
                      isActive
                        ? "bg-emerald/[0.09] text-emerald border border-emerald-border shadow-[0_0_18px_-5px_rgba(0,212,160,0.16)]"
                        : "text-text-tertiary hover:text-text hover:bg-bg-hover"
                    )
                  }
                >
                  {({ isActive }) => (
                    <>
                      {isActive && (
                        <div className="absolute left-0 top-1/2 -translate-y-1/2 w-1 h-5 rounded-r-full bg-emerald shadow-[0_0_12px_rgba(0,212,160,0.5)]" />
                      )}
                      <item.icon size={19} className={cn("shrink-0", isActive ? "text-emerald" : "text-text-muted")} />
                      <span className="flex-1 truncate">{item.label}</span>
                      <ChevronRight size={14} className={cn("shrink-0", isActive ? "text-emerald" : "text-text-muted")} />
                    </>
                  )}
                </NavLink>
              ))}
            </nav>
            <div className="p-3 border-t border-border/60 space-y-2">
              <div className="flex items-center gap-2.5 px-3 py-2.5 rounded-xl bg-bg-card/50 border border-border/60">
                <div
                  className={cn(
                    "w-2 h-2 rounded-full shrink-0",
                    connected ? "bg-emerald animate-pulse" : "bg-text-muted"
                  )}
                />
                <span className="text-xs text-text-tertiary truncate">
                  {connected ? "实时已连接" : "连接中..."}
                </span>
              </div>
              <button
                onClick={handleLogout}
                className="w-full flex items-center gap-2 px-3 py-3.5 rounded-xl bg-bg-hover text-text-secondary text-sm hover:bg-border-soft min-h-[48px]"
              >
                <LogOut size={16} />
                退出登录
              </button>
            </div>
          </aside>
        </>
      )}

      {/* 主区 */}
      <div className="flex-1 flex flex-col min-w-0">
        <header className="sticky top-0 z-30 h-16 border-b border-border/60 bg-bg-soft/40 backdrop-blur-2xl page-header-gradient flex items-center justify-between px-4 md:px-6">
          <div className="flex items-center gap-3 min-w-0">
            <button
              onClick={() => setDrawerOpen(true)}
              className="md:hidden w-11 h-11 rounded-xl bg-bg-hover/80 text-text-secondary flex items-center justify-center touch-target border border-border/60"
              aria-label="打开菜单"
            >
              <Menu size={20} />
            </button>
            <div className="min-w-0">
              <div className="flex items-center gap-2">
                <span className="text-sm font-bold text-text truncate">
                  {currentPage || "DC Quant"}
                </span>
              </div>
              <div className="hidden sm:flex items-center gap-2 text-xs text-text-muted">
                <span>欢迎,</span>
                <span className="text-text-secondary truncate">{user?.display_name || user?.username}</span>
                <span className="px-1.5 py-0.5 rounded-md bg-bg-hover border border-border/60 text-text-tertiary">
                  {user?.role === "admin" ? "管理员" : "客户"}
                </span>
              </div>
            </div>
          </div>
          <div className="flex items-center gap-2 md:gap-3 shrink-0">
            {/* API账户切换 - 仅多API客户可见 */}
            {user?.role === "customer" && <AccountFilter />}

            {/* 实时连接状态 - 右上角常驻显示 */}
            <div className={cn(
              "flex items-center gap-1.5 px-3 py-1.5 rounded-lg text-xs font-bold border transition-all",
              connected
                ? "bg-emerald/10 border-emerald-border"
                : "bg-bg-card/50 border-border/60"
            )}>
              <div
                className={cn(
                  "w-2 h-2 rounded-full shrink-0",
                  connected ? "bg-emerald animate-pulse shadow-[0_0_8px_rgba(0,212,160,0.5)]" : "bg-text-muted"
                )}
              />
              <span
                className={cn(
                  "text-xs font-bold hidden sm:inline",
                  connected ? "text-emerald" : "text-text-muted"
                )}
              >
                {connected ? "实时已连接" : "连接中..."}
              </span>
            </div>

            {sourceStatus && (
              <div
                className={cn(
                  "flex items-center justify-center border transition-all",
                  user?.role === "admin"
                    ? "hidden lg:flex gap-1.5 px-3 py-1.5 rounded-lg text-xs font-bold"
                    : "w-8 h-8 rounded-xl",
                  sourceLevel === "green"
                    ? "bg-emerald/10 border-emerald-border text-emerald"
                    : sourceLevel === "yellow"
                      ? "bg-gold/10 border-gold/30 text-gold"
                      : "bg-rose/10 border-rose/25 text-rose"
                )}
                title={
                  sourceHealthy
                    ? `转发源连接正常，最后心跳: ${sourceStatus.last_heartbeat_ack_at || "暂无"}`
                    : sourceStatus.last_error || sourceStatus.label || "转发源未连接或心跳异常"
                }
                aria-label={sourceLabel}
              >
                <div
                  className={cn(
                    "rounded-full shrink-0",
                    user?.role === "admin" ? "w-2 h-2" : "w-3 h-3",
                    sourceLevel === "green"
                      ? "bg-emerald animate-pulse shadow-[0_0_8px_rgba(0,212,160,0.5)]"
                      : sourceLevel === "yellow"
                        ? "bg-gold animate-pulse shadow-[0_0_8px_rgba(240,180,41,0.45)]"
                        : "bg-rose animate-pulse shadow-[0_0_8px_rgba(244,63,94,0.45)]"
                  )}
                />
                {user?.role === "admin" && <span>{sourceLabel}</span>}
              </div>
            )}

            {/* 急停开关 - 仅客户可见,一键开启/停止 */}
            {user?.role === "customer" && (
              <button
                onClick={handleToggleEmergencyStop}
                disabled={esLoading}
                className={cn(
                  "flex items-center gap-1.5 px-3 py-1.5 rounded-lg text-xs font-bold transition-all border touch-target",
                  user?.emergency_stop
                    ? "bg-rose/15 text-rose border-rose/30 shadow-[0_0_14px_-4px_rgba(244,63,94,0.35)]"
                    : "bg-emerald/10 text-emerald border-emerald-border hover:bg-emerald/15",
                  esLoading && "opacity-60 cursor-wait"
                )}
                title={user?.emergency_stop ? "点击恢复跟单" : "点击紧急停止开仓"}
              >
                <Power size={14} className={user?.emergency_stop ? "animate-pulse" : ""} />
                <span className="hidden sm:inline">{user?.emergency_stop ? "急停中" : "运行中"}</span>
              </button>
            )}

            {/* 授权状态 */}
            {user?.role === "customer" &&
              (authorized ? (
                <span className="chip bg-emerald/10 text-emerald border border-emerald-border hidden lg:inline-flex">
                  <ShieldCheck size={13} /> 已授权
                </span>
              ) : (
                <span className="chip bg-rose/10 text-rose border border-rose/20 hidden lg:inline-flex">
                  <ShieldAlert size={13} /> 未授权
                </span>
              ))}
            <button
              onClick={handleLogout}
              className="hidden md:inline-flex btn-ghost px-3 py-1.5"
            >
              <LogOut size={15} /> 退出
            </button>
          </div>
        </header>

        <main className="flex-1 overflow-y-auto p-4 md:p-6 pb-36 md:pb-6 scroll-pb-36 md:scroll-pb-6">
          <div className="animate-fadeIn">{children}</div>
        </main>

        {/* 移动端底部导航 */}
        {bottomItems.length > 0 && (
          <nav className="md:hidden fixed bottom-0 left-0 right-0 z-30 border-t border-border/60 bg-bg-soft/95 backdrop-blur-2xl safe-bottom">
            <div className="flex items-stretch justify-around">
              {bottomItems.map((item) => (
                <NavLink
                  key={item.to}
                  to={item.to}
                  className={({ isActive }) =>
                    cn(
                      "flex flex-col items-center justify-center flex-1 py-2 min-h-[60px] text-xs transition-all relative",
                      isActive ? "text-emerald" : "text-text-muted hover:text-text-secondary"
                    )
                  }
                >
                  {({ isActive }) => (
                    <>
                      {isActive && (
                        <div className="absolute top-0 left-1/2 -translate-x-1/2 w-9 h-0.5 rounded-b-full bg-emerald shadow-[0_0_12px_rgba(0,212,160,0.55)]" />
                      )}
                      <item.icon size={22} strokeWidth={isActive ? 2.5 : 2} />
                      <span className="mt-1 font-semibold">{item.label}</span>
                    </>
                  )}
                </NavLink>
              ))}
            </div>
          </nav>
        )}
      </div>
    </div>
  );
}
