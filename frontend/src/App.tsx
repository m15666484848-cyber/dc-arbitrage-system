import { useEffect } from "react";
import { Navigate, Route, Routes, useLocation } from "react-router-dom";
import { useAuthStore } from "@/stores/auth";
import { ToastProvider } from "@/components/ui/Toast";
import { ErrorBoundary } from "@/components/ErrorBoundary";
import { Layout } from "@/components/Layout";
import { API } from "@/api/client";
import LoginPage from "@/pages/Login";
import RegisterPage from "@/pages/Register";
import Dashboard from "@/pages/Dashboard";
import DataChartsPage from "@/pages/DataCharts";
import KolsPage from "@/pages/Kols";
import KolDetailPage from "@/pages/KolDetail";
import SignalsPage from "@/pages/Signals";
import PositionsPage from "@/pages/Positions";
import TradesPage from "@/pages/Trades";
import DailyStatsPage from "@/pages/DailyStats";
import StrategiesPage from "@/pages/Strategies";
import SettingsPage from "@/pages/Settings";
import AdminCustomers from "@/pages/admin/Customers";
import AdminKols from "@/pages/admin/KolMgmt";
import AdminSignals from "@/pages/admin/AdminSignals";
import AdminSettings from "@/pages/admin/Settings";
import ProfitStatsPage from "@/pages/admin/ProfitStats";
import AdminDiagnosis from "@/pages/admin/Diagnosis";
import AdminSimulator from "@/pages/admin/Simulator";

function Protected({
  children,
  role,
  requireSignalSummary = false,
}: {
  children: JSX.Element;
  role?: "admin" | "customer";
  requireSignalSummary?: boolean;
}) {
  const { user } = useAuthStore();
  const loc = useLocation();
  if (!user) return <Navigate to="/login" state={{ from: loc }} replace />;
  if (role && user.role !== role) {
    if (user.role === "admin") return <Navigate to="/admin/customers" replace />;
    return <Navigate to="/dashboard" replace />;
  }
  if (requireSignalSummary && user.role === "customer" && !user.show_signal_summary) {
    return <Navigate to="/dashboard" replace />;
  }
  return <Layout>{children}</Layout>;
}

function InitGate({ children }: { children: React.ReactNode }) {
  const { token, user, initialized, setAuth, setUser, markInitialized, logout } = useAuthStore();

  useEffect(() => {
    if (initialized) return;
    // S9修复: 页面刷新时通过HttpOnly Cookie中的refresh_token获取新access token
    // token不再持久化到localStorage,仅在内存中
    if (token) {
      // 内存中已有token,直接验证
      API.me()
        .then((u: any) => setUser(u))
        .catch(() => logout())
        .finally(() => markInitialized());
    } else {
      // 尝试通过refresh token恢复会话
      API.refreshToken()
        .then((res: any) => {
          setAuth(res.access_token, {
            id: res.user_id,
            username: res.username,
            role: res.role,
            display_name: res.display_name,
            authorization: res.authorization,
            show_signal_summary: res.show_signal_summary,
            emergency_stop: res.emergency_stop,
          });
        })
        .catch(() => {
          // refresh失败,用户需重新登录
        })
        .finally(() => markInitialized());
    }
    // S9修复: 处理管理员模拟登录的impersonate_token (URL hash传递)
    const hash = window.location.hash;
    const match = hash.match(/impersonate_token=([^&]+)/);
    if (match) {
      const impToken = decodeURIComponent(match[1]);
      // 清除URL hash中的token
      window.location.hash = "";
      // 使用impersonate token获取用户信息
      useAuthStore.getState().setAuth(impToken, { id: 0, username: "", role: "customer" });
      API.me()
        .then((u: any) => setUser(u))
        .catch(() => logout())
        .finally(() => markInitialized());
    }
  }, [token, initialized, setUser, setAuth, markInitialized, logout]);

  if (!initialized) {
    return (
      <div className="min-h-screen flex items-center justify-center bg-bg">
        <div className="text-slate-500 text-sm">加载中...</div>
      </div>
    );
  }
  return <>{children}</>;
}


function WildcardRedirect() {
  const { user, token } = useAuthStore();
  const dest = user?.role === "admin" ? "/admin/customers" : token ? "/dashboard" : "/login";
  return <Navigate to={dest} replace />;
}

export default function App() {
  return (
    <ErrorBoundary>
      <ToastProvider>
        <InitGate>
          <Routes>
            <Route path="/login" element={<LoginPage />} />
            <Route path="/register" element={<RegisterPage />} />
            <Route path="/dashboard" element={<Protected><Dashboard /></Protected>} />
            <Route path="/data-charts" element={<Protected role="customer"><DataChartsPage /></Protected>} />
            <Route path="/kols" element={<Protected><KolsPage /></Protected>} />
            <Route path="/kols/:id" element={<Protected><KolDetailPage /></Protected>} />
            <Route path="/signals" element={<Protected requireSignalSummary><SignalsPage /></Protected>} />
            <Route path="/positions" element={<Protected><PositionsPage /></Protected>} />
            <Route path="/trades" element={<Protected><TradesPage /></Protected>} />
            <Route path="/daily-stats" element={<Protected><DailyStatsPage /></Protected>} />
            <Route path="/strategies" element={<Protected><StrategiesPage /></Protected>} />
            <Route path="/settings" element={<Protected><SettingsPage /></Protected>} />
            <Route path="/admin/customers" element={<Protected role="admin"><AdminCustomers /></Protected>} />
            <Route path="/admin/profit-stats" element={<Protected role="admin"><ProfitStatsPage /></Protected>} />
            <Route path="/admin/kols" element={<Protected role="admin"><AdminKols /></Protected>} />
            <Route path="/admin/signals" element={<Protected role="admin"><AdminSignals /></Protected>} />
            <Route path="/admin/diagnosis" element={<Protected role="admin"><AdminDiagnosis /></Protected>} />
            <Route path="/admin/simulator" element={<Protected role="admin"><AdminSimulator /></Protected>} />
            <Route path="/admin/settings" element={<Protected role="admin"><AdminSettings /></Protected>} />
              {/* S16: subscribe to store instead of getState() */}
              <Route path="*" element={<WildcardRedirect />} />
          </Routes>
        </InitGate>
      </ToastProvider>
    </ErrorBoundary>
  );
}
