import { FormEvent, useEffect, useState } from "react";
import { useNavigate } from "react-router-dom";
import { Activity, Lock, User, Eye, EyeOff } from "lucide-react";
import { API } from "@/api/client";
import { useAuthStore } from "@/stores/auth";
import { useToast } from "@/components/ui/Toast";

export default function LoginPage() {
  const [username, setUsername] = useState("");
  const [password, setPassword] = useState("");
  const [showPwd, setShowPwd] = useState(false);
  const [loading, setLoading] = useState(false);
  const { setAuth, user, token } = useAuthStore();
  const nav = useNavigate();
  const { push } = useToast();

  useEffect(() => {
    if (token && user) {
      nav(user.role === "admin" ? "/admin/customers" : "/dashboard", { replace: true });
    }
  }, [token, user, nav]);

  const goRegister = (e: React.MouseEvent) => {
    e.preventDefault();
    nav("/register");
  };

  const submit = async (e: FormEvent) => {
    e.preventDefault();
    if (!username || !password) {
      push("error", "请输入用户名和密码");
      return;
    }
    setLoading(true);
    try {
      const res: any = await API.login(username, password);
      setAuth(res.access_token, {
        id: res.user_id,
        username: res.username,
        role: res.role,
        display_name: res.display_name,
        authorization: res.authorization,
        show_signal_summary: res.show_signal_summary,
      });
      push("success", `欢迎回来, ${res.display_name || res.username}`);
      nav(res.role === "admin" ? "/admin/customers" : "/dashboard", { replace: true });
    } catch (e: any) {
      let msg = "登录失败, 请重试";
      const resp = e?.response?.data;
      if (!e?.response) {
        msg = "网络错误, 无法连接服务器";
      } else if (typeof resp?.detail === "string") {
        msg = resp.detail;
      } else if (Array.isArray(resp?.detail) && resp.detail.length > 0) {
        msg = resp.detail[0]?.msg || resp.detail[0] || msg;
      } else if (resp?.message) {
        msg = resp.message;
      }
      push("error", msg);
    } finally {
      setLoading(false);
    }
  };

  return (
    <div className="min-h-screen flex bg-bg">
      <div className="hidden lg:flex lg:w-[55%] flex-col justify-between p-12 relative overflow-hidden">
        <div className="absolute inset-0 bg-gradient-to-br from-accent/10 via-transparent to-accent-glow/5" />
        <div className="absolute -top-20 -left-20 w-80 h-80 bg-accent/15 rounded-full blur-3xl" />
        <div className="absolute -bottom-20 -right-10 w-64 h-64 bg-accent-glow/10 rounded-full blur-3xl" />

        <div className="relative">
          <div className="flex items-center gap-2.5 mb-12">
            <div className="w-10 h-10 rounded-xl bg-gradient-to-br from-accent to-accent-glow flex items-center justify-center shadow-glow">
              <Activity size={22} className="text-slate-900" />
            </div>
            <div>
              <div className="text-lg font-bold tracking-wide text-slate-100">DC QUANT</div>
              <div className="text-[10px] text-slate-500 tracking-widest uppercase">Auto Trading System</div>
            </div>
          </div>
        </div>

        <div className="relative flex-1 flex flex-col justify-center max-w-md">
          <h1 className="text-5xl font-bold leading-tight mb-4">
            捕捉每一次
            <span className="gradient-text"> 交易信号</span>
          </h1>
          <p className="text-slate-400 mb-8 leading-relaxed">
            Discord KOL 实时跟单 · 多交易所统一执行 · 专业量化风控, 让每一次机会都不被错过。
          </p>

          <div className="grid grid-cols-2 gap-4 text-sm text-slate-400">
            <div className="flex items-center gap-2">
              <span className="w-1.5 h-1.5 rounded-full bg-accent" />
              毫秒级执行
            </div>
            <div className="flex items-center gap-2">
              <span className="w-1.5 h-1.5 rounded-full bg-accent" />
              多策略并行
            </div>
            <div className="flex items-center gap-2">
              <span className="w-1.5 h-1.5 rounded-full bg-accent" />
              自动风控保护
            </div>
            <div className="flex items-center gap-2">
              <span className="w-1.5 h-1.5 rounded-full bg-accent" />
              实时数据分析
            </div>
          </div>
        </div>

        <div className="relative text-xs text-slate-600">
          © 2025 DC Quant · Professional copy · High trading infrastructure.
        </div>
      </div>

      <div className="flex-1 flex items-center justify-center p-6 relative">
        <div className="absolute top-6 left-6 lg:hidden flex items-center gap-2">
          <div className="w-8 h-8 rounded-lg bg-gradient-to-br from-accent to-accent-glow flex items-center justify-center shadow-glow">
            <Activity size={18} className="text-slate-900" />
          </div>
          <span className="font-bold text-slate-100">DC QUANT</span>
        </div>

        <div className="w-full max-w-sm glass shadow-card p-8">
          <div className="text-center mb-7">
            <div className="w-14 h-14 mx-auto rounded-2xl bg-gradient-to-br from-accent to-accent-glow flex items-center justify-center shadow-glow mb-3">
              <Activity size={26} className="text-slate-900" />
            </div>
            <h2 className="text-xl font-bold text-slate-100">欢迎回来</h2>
            <p className="text-sm text-slate-500 mt-1">登录您的 DC Quant 账户</p>
          </div>

          <form onSubmit={submit} className="space-y-4">
            <div>
              <label className="block text-xs font-medium text-slate-400 mb-1.5">账号</label>
              <div className="relative">
                <User size={16} className="absolute left-3 top-1/2 -translate-y-1/2 text-slate-500" />
                <input
                  className="input pl-9"
                  placeholder="输入用户名"
                  value={username}
                  onChange={(e) => setUsername(e.target.value)}
                  autoFocus
                />
              </div>
            </div>
            <div>
              <label className="block text-xs font-medium text-slate-400 mb-1.5">密码</label>
              <div className="relative">
                <Lock size={16} className="absolute left-3 top-1/2 -translate-y-1/2 text-slate-500" />
                <input
                  type={showPwd ? "text" : "password"}
                  className="input pl-9 pr-10"
                  placeholder="输入密码"
                  value={password}
                  onChange={(e) => setPassword(e.target.value)}
                />
                <button
                  type="button"
                  onClick={() => setShowPwd((v) => !v)}
                  className="absolute right-3 top-1/2 -translate-y-1/2 text-slate-500 hover:text-slate-300"
                >
                  {showPwd ? <EyeOff size={16} /> : <Eye size={16} />}
                </button>
              </div>
            </div>
            <button
              type="submit"
              className="w-full bg-gradient-to-r from-accent to-accent-glow text-white rounded-xl py-2.5 text-sm font-medium hover:shadow-glow transition-all disabled:opacity-50 disabled:cursor-not-allowed"
              disabled={loading || !username || !password}
            >
              {loading ? "登录中..." : "登 录"}
            </button>
          </form>

          <p className="text-xs text-slate-500 text-center mt-4">
            管理员请联系系统管理员开通账号
          </p>
          <div className="text-center mt-3">
            <a
              href="/register"
              onClick={goRegister}
              className="text-sm text-accent-glow hover:text-amber-300 transition font-medium cursor-pointer"
            >
              还没有账号? 点击注册 →
            </a>
          </div>
        </div>
      </div>
    </div>
  );
}
