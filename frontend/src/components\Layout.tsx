import { ReactNode, useEffect, useState } from "react";
import { NavLink, useNavigate } from "react-router-dom";
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
} from "lucide-react";
import { useAuthStore } from "@/stores/auth";
import { wsClient } from "@/api/ws";
import { cn } from "@/lib/utils";

interface NavItem {
  to: string;
  label: string;
  icon: any;
  roles: ("admin" | "customer")[];
  section?: string;
}

const NAV: NavItem[] = [
  { to: "/dashboard", label: "仪表盘", icon: LayoutDashboard, roles: ["customer"], section: "交易" },
  { to: "/kols", label: "KOL 排行", icon: Crown, roles: ["customer"], section: "交易" },
  { to: "/signals", label: "信号汇总", icon: Radio, roles: ["customer", "admin"], section: "交易" },
  { to: "/positions", label: "持仓管理", icon: Wallet, roles: ["customer"], section: "交易" },
  { to: "/trades", label: "交易记录", icon: History, roles: ["customer"], section: "交易" },
  { to: "/strategies", label: "策略管理", icon: SlidersHorizontal, roles: ["customer"], section: "交易" },
  { to: "/settings", label: "交易设置", icon: Settings, roles: ["customer"], section: "交易" },
  { to: "/admin/customers", label: "客户管理", icon: Users, roles: ["admin"], section: "管理" },
  { to: "/admin/kols", label: "KOL 管理", icon: Crown, roles: ["admin"], section: "管理" },
  { to: "/admin/signals", label: "信号监控", icon: Radio, roles: ["admin"], section: "管理" },
  { to: "/admin/settings", label: "系统设置", icon: Cog, roles: ["admin"], section: "管理" },
  { to: "/admin/symbol-notional", label: "分类倍率", icon: Gauge, roles: ["admin"], section: "管理" },
];

const BOTTOM_NAV: NavItem[] = [
  { to: "/dashboard", label: "仪表盘", icon: LayoutDashboard, roles: ["customer"] },
  { to: "/kols", label: "KOL", icon: Crown, roles: ["customer"] },
  { to: "/positions", label: "持仓", icon: Wallet, roles: ["customer"] },
  { to: "/signals", label: "信号", icon: Radio, roles: ["customer", "admin"] },
  { to: "/settings", label: "设置", icon: Settings, roles: ["customer"] },
  { to: "/admin/customers", label: "客户", icon: Users, roles: ["admin"] },
  { to: "/admin/kols", label: "KOL", icon: Crown, roles: ["admin"] },
  { to: "/admin/signals", label: "监控", icon: Radio, roles: ["admin"] },
  { to: "/admin/settings", label: "系统", icon: Cog, roles: ["admin"] },
];

export function Layout({ children }: { children: ReactNode }) {
  const { user, logout } = useAuthStore();
  const nav = useNavigate();
  const [connected, setConnected] = useState(false);
  const [drawerOpen, setDrawerOpen] = useState(false);

  useEffect(() => {
    wsClient.connect();
    const off = wsClient.on((event) => {
      if (event === "connected") setConnected(true);
    });
    return () => {
      off();
      wsClient.close();
    };
  }, []);

  const isAdmin = user?.role === "admin";
  const items = NAV.filter((n) => {
    if (!user) return false;
    if (isAdmin) return true;
    return n.roles.includes(user.role);
  });
  const bottomItems = BOTTOM_NAV.filter((n) => {
    if (!user) return false;
    if (isAdmin) return n.roles.includes("admin");
    return n.roles.includes(user.role);
  });
  const authorized = user?.role === "admin" || user?.authorization?.authorized;

  const sections = items.reduce<Record<string, NavItem[]>>((acc, item) => {
    const key = item.section || "其他";
    if (!acc[key]) acc[key] = [];
    acc[key].push(item);
    return acc;
  }, {});

  const closeDrawer = () => setDrawerOpen(false);

  const handleLogout = () => {
    logout();
    wsClient.close();
    nav("/login");
  };

  const renderNavItems = (onClick?: () => void) => {
    const sectionOrder = ["交易", "管理", "其他"];
    return sectionOrder
      .filter((s) => sections[s])
      .map((section) => (
        <div key={section} className="mb-3">
          <div className="px-3 mb-1.5 text-[10px] font-semibold text-slate-600 uppercase tracking-wider">
            {section}
          </div>
          {sections[section].map((item) => (
            <NavLink
              key={item.to}
              to={item.to}
              onClick={onClick}
              className={({ isActive }) =>
                cn(
                  "flex items-center gap-3 px-3 py-2.5 rounded-xl text-sm transition-all mb-0.5",
                  isActive
                    ? "bg-gradient-to-r from-accent/20 to-transparent text-accent-glow border border-accent/30"
                    : "text-slate-400 hover:text-slate-100 hover:bg-bg-hover"
                )
              }
            >
              <item.icon size={17} />
              {item.label}
            </NavLink>
          ))}
        </div>
      ));
  };

  return (
    <div className="min-h-screen flex bg-grid-pattern bg-[size:32px_32px]">
      <aside className="hidden md:flex w-60 shrink-0 border-r border-border bg-bg-soft/50 backdrop-blur-xl flex-col">
        <div className="h-16 flex items-center gap-2.5 px-5 border-b border-border">
          <div className="w-9 h-9 rounded-xl bg-gradient-to-br from-accent to-accent-glow flex items-center justify-center shadow-glow">
            <Activity size={18} className="text-white" />
          </div>
          <div>
            <div className="text-sm font-bold gradient-text leading-tight">DC 量化</div>
            <div className="text-[10px] text-slate-500 leading-tight">KOL 跟单系统</div>
          </div>
        </div>
        <nav className="flex-1 p-3 overflow-y-auto">
          {renderNavItems()}
        </nav>
        <div className="p-3 border-t border-border space-y-2">
          <div className="flex items-center gap-2 px-3 py-2 rounded-xl bg-bg-card/50">
            <div
              className={cn(
                "w-2 h-2 rounded-full",
                connected ? "bg-profit animate-pulse" : "bg-slate-600"
              )}
            />
            <span className="text-xs text-slate-400">{connected ? "实时已连接" : "连接中..."}</span>
          </div>
          {user && (
            <div className="px-3 py-2 rounded-xl bg-bg-hover/50 text-xs text-slate-400">
              <div className="font-medium text-slate-200 truncate">{user.display_name || user.username}</div>
              <div className="mt-0.5">{user.role === "admin" ? "管理员" : "客户"}</div>
            </div>
          )}
        </div>
      </aside>

      {drawerOpen && (
        <>
          <div
            className="fixed inset-0 bg-black/60 backdrop-blur-sm z-40 md:hidden"
            onClick={closeDrawer}
          />
          <aside className="fixed inset-y-0 left-0 w-72 max-w-[85%] border-r border-border bg-bg-soft z-50 flex flex-col animate-[slideIn_.25s_ease-out] md:hidden">
            <div className="h-16 flex items-center justify-between px-5 border-b border-border">
              <div className="flex items-center gap-2.5">
                <div className="w-9 h-9 rounded-xl bg-gradient-to-br from-accent to-accent-glow flex items-center justify-center shadow-glow">
                  <Activity size={18} className="text-white" />
                </div>
                <div>
                  <div className="text-sm font-bold gradient-text leading-tight">DC 量化</div>
                  <div className="text-[10px] text-slate-500 leading-tight">KOL 跟单系统</div>
                </div>
              </div>
              <button
                onClick={closeDrawer}
                className="w-9 h-9 rounded-lg hover:bg-bg-hover flex items-center justify-center text-slate-400"
                aria-label="关闭菜单"
              >
                <X size={18} />
              </button>
            </div>
            <nav className="flex-1 p-3 overflow-y-auto">
              {renderNavItems(closeDrawer)}
            </nav>
            <div className="p-3 border-t border-border space-y-2">
              <div className="flex items-center gap-2 px-3 py-2 rounded-xl bg-bg-card/50">
                <div
                  className={cn(
                    "w-2 h-2 rounded-full",
                    connected ? "bg-profit animate-pulse" : "bg-slate-600"
                  )}
                />
                <span className="text-xs text-slate-400">{connected ? "实时已连接" : "连接中..."}</span>
              </div>
              <button
                onClick={handleLogout}
                className="w-full flex items-center gap-2 px-3 py-3 rounded-xl bg-bg-hover text-slate-300 text-sm hover:bg-border-soft min-h-[44px]"
              >
                <LogOut size={16} />
                退出登录
              </button>
            </div>
          </aside>
        </>
      )}

      <div className="flex-1 flex flex-col min-w-0">
        <header className="sticky top-0 z-30 h-16 border-b border-border bg-bg-soft/60 backdrop-blur-xl flex items-center justify-between px-4 md:px-6">
          <div className="flex items-center gap-3 min-w-0">
            <button
              onClick={() => setDrawerOpen(true)}
              className="md:hidden w-10 h-10 rounded-lg bg-bg-hover text-slate-300 flex items-center justify-center"
              aria-label="打开菜单"
            >
              <Menu size={20} />
            </button>
            <div className="min-w-0">
              <span className="text-sm text-slate-400">欢迎,</span>
              <span className="text-sm font-semibold text-slate-100 ml-1.5 truncate">
                {user?.display_name || user?.username}
              </span>
              <span className="ml-2 text-[10px] px-1.5 py-0.5 rounded-md bg-bg-hover text-slate-400 align-middle">
                {user?.role === "admin" ? "管理员" : "客户"}
              </span>
            </div>
          </div>
          <div className="flex items-center gap-2 md:gap-3 shrink-0">
            {user?.role === "customer" &&
              (authorized ? (
                <span className="chip bg-profit/15 text-profit hidden sm:inline-flex">
                  <ShieldCheck size={13} /> 已授权
                </span>
              ) : (
                <span className="chip bg-loss/15 text-loss hidden sm:inline-flex">
                  <ShieldAlert size={13} /> 未授权
                </span>
              ))}
            <div
              className={cn(
                "md:hidden w-2 h-2 rounded-full",
                connected ? "bg-profit animate-pulse" : "bg-slate-600"
              )}
              title={connected ? "实时已连接" : "连接中..."}
            />
            <button
              onClick={handleLogout}
              className="btn-ghost px-3 py-1.5 hidden md:inline-flex"
            >
              <LogOut size={15} /> 退出
            </button>
          </div>
        </header>

        <main className="flex-1 overflow-y-auto p-4 md:p-6 pb-24 md:pb-6">
          {children}
        </main>

        {bottomItems.length > 0 && (
          <nav className="md:hidden fixed bottom-0 left-0 right-0 z-30 border-t border-border bg-bg-soft/95 backdrop-blur-xl safe-bottom">
            <div className="flex items-stretch justify-around">
              {bottomItems.map((item) => (
                <NavLink
                  key={item.to}
                  to={item.to}
                  className={({ isActive }) =>
                    cn(
                      "flex flex-col items-center justify-center flex-1 py-2.5 min-h-[56px] text-[11px] transition-colors",
                      isActive
                        ? "text-accent-glow"
                        : "text-slate-500 hover:text-slate-300"
                    )
                  }
                >
                  {({ isActive }) => (
                    <>
                      <item.icon size={20} strokeWidth={isActive ? 2.5 : 2} />
                      <span className="mt-1">{item.label}</span>
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
