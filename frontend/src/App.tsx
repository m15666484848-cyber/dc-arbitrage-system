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
import SymbolNotionalPage from "@/pages/admin/SymbolNotional";
import ProfitStatsPage from "@/pages/admin/ProfitStats";
import AdminDiagnosis from "@/pages/admin/Diagnosis";
import AdminSimulator from "@/pages/admin/Simulator";
import ShadowComparison from "@/pages/admin/ShadowComparison";

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
  const { token, user, initialized, setUser, markInitialized, logout } = useAuthStore();

  useEffect(() => {
    if (initialized) return;
    if (token) {
      API.me()
        .then((u: any) => setUser(u))
        .catch(() => logout())
        .finally(() => markInitialized());
    } else {
      markInitialized();
    }
  }, [token, initialized, setUser, markInitialized, logout]);

  if (!initialized) {
    return (
      <div className="min-h-screen flex items-center justify-center bg-bg">
        <div className="text-slate-500 text-sm">加载中...</div>
      </div>
    );
  }
  return <>{children}</>;
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
            <Route path="/admin/shadow-comparison" element={<Protected role="admin"><ShadowComparison /></Protected>} />
            <Route path="/admin/settings" element={<Protected role="admin"><AdminSettings /></Protected>} />
            <Route path="/admin/symbol-notional" element={<Protected role="admin"><SymbolNotionalPage /></Protected>} />
            <Route
              path="*"
              element={
                <Navigate
                  to={useAuthStore.getState().user?.role === "admin" ? "/admin/customers" : useAuthStore.getState().token ? "/dashboard" : "/login"}
                  replace
                />
              }
            />
          </Routes>
        </InitGate>
      </ToastProvider>
    </ErrorBoundary>
  );
}
