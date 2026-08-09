import { FormEvent, useEffect, useState } from "react";
import { useNavigate } from "react-router-dom";
import { Activity, Lock, User, UserPlus, ArrowLeft, Eye, EyeOff, CheckCircle2, Ticket } from "lucide-react";
import { API } from "@/api/client";
import { useAuthStore } from "@/stores/auth";
import { useToast } from "@/components/ui/Toast";

export default function RegisterPage() {
  const [username, setUsername] = useState("");
  const [password, setPassword] = useState("");
  const [displayName, setDisplayName] = useState("");
  const [inviteCode, setInviteCode] = useState("");
  const [showPwd, setShowPwd] = useState(false);
  const [loading, setLoading] = useState(false);
  const [success, setSuccess] = useState(false);
  const nav = useNavigate();
  const { user, token } = useAuthStore();
  const { push } = useToast();

  useEffect(() => {
    if (token && user) {
      nav(user.role === "admin" ? "/admin/customers" : "/dashboard", { replace: true });
    }
  }, [token, user, nav]);

  // 从 URL ?code=XXX 自动填入邀请码
  useEffect(() => {
    const params = new URLSearchParams(window.location.search);
    const code = params.get("code");
    if (code) setInviteCode(code);
  }, []);

  const goLogin = (e: React.MouseEvent) => {
    e.preventDefault();
    nav("/login");
  };

  const submit = async (e: FormEvent) => {
    e.preventDefault();
    if (!username || !password) {
      push("error", "请输入用户名和密码");
      return;
    }
    if (password.length < 6) {
      push("error", "密码至少 6 位");
      return;
    }
    setLoading(true);
    try {
      await API.register(username, password, displayName || undefined, inviteCode || undefined);
      setSuccess(true);
      push("success", "注册成功! 等待管理员审批后即可登录");
    } catch (e: any) {
      let msg = "注册失败, 请重试";
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

  if (success) {
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
            <div className="w-20 h-20 mx-auto rounded-2xl bg-gradient-to-br from-green-500 to-emerald-400 flex items-center justify-center shadow-glow mb-6">
              <CheckCircle2 size={40} className="text-white" />
            </div>
            <h1 className="text-3xl font-bold text-center gradient-text mb-3">注册成功</h1>
            <p className="text-sm text-slate-400 text-center leading-relaxed mb-6">
              您的账号已提交, 状态为 <span className="text-amber-400 font-semibold">待审批</span>。<br />
              请耐心等待管理员审批, 审批通过后即可登录使用。
            </p>
            <div className="bg-slate-800/60 rounded-xl p-4 text-xs text-slate-400 mb-6 border border-border">
              💡 审批通常在 24 小时内完成, 如有疑问请联系客服。
            </div>
            <button
              onClick={() => nav("/login")}
              className="w-full bg-gradient-to-r from-accent to-accent-glow text-white rounded-xl py-3 text-sm font-medium hover:shadow-glow transition-all"
            >
              返回登录
            </button>
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
            <div className="text-center mb-6">
              <div className="w-14 h-14 mx-auto rounded-2xl bg-gradient-to-br from-green-500 to-emerald-400 flex items-center justify-center shadow-glow mb-3">
                <CheckCircle2 size={26} className="text-white" />
              </div>
              <h2 className="text-xl font-bold text-slate-100">注册成功</h2>
              <p className="text-sm text-slate-500 mt-1">等待管理员审批后即可登录</p>
            </div>
            <div className="bg-slate-800/60 rounded-xl p-4 text-xs text-slate-400 mb-6 border border-border">
              ⚠️ 账号状态: <b className="text-amber-300">待审批</b> — 管理员审批通过后方可登录交易。
            </div>
            <button
              onClick={() => nav("/login")}
              className="w-full bg-gradient-to-r from-accent to-accent-glow text-white rounded-xl py-2.5 text-sm font-medium hover:shadow-glow transition-all"
            >
              返回登录
            </button>
          </div>
        </div>
      </div>
    );
  }

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
            开启您的
            <span className="gradient-text"> 跟单之旅</span>
          </h1>
          <p className="text-slate-400 mb-8 leading-relaxed">
            注册账号后, 管理员审批通过即可开始体验 Discord KOL 实时跟单 · 多交易所统一执行 · 专业量化风控。
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
              <UserPlus size={26} className="text-slate-900" />
            </div>
            <h2 className="text-xl font-bold text-slate-100">注册账号</h2>
            <p className="text-sm text-slate-500 mt-1">创建客户账号 · 等待管理员审批</p>
          </div>

          <form onSubmit={submit} className="space-y-4">
            <div>
              <label className="block text-xs font-medium text-slate-400 mb-1.5">账号</label>
              <div className="relative">
                <User size={16} className="absolute left-3 top-1/2 -translate-y-1/2 text-slate-500" />
                <input
                  className="input pl-9"
                  placeholder="设置登录用户名"
                  value={username}
                  onChange={(e) => setUsername(e.target.value)}
                  autoFocus
                  required
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
                  placeholder="设置密码 (至少 6 位)"
                  value={password}
                  onChange={(e) => setPassword(e.target.value)}
                  required
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
            <div>
              <label className="block text-xs font-medium text-slate-400 mb-1.5">昵称 <span className="text-slate-600">(选填)</span></label>
              <div className="relative">
                <UserPlus size={16} className="absolute left-3 top-1/2 -translate-y-1/2 text-slate-500" />
                <input
                  className="input pl-9"
                  placeholder="设置显示昵称"
                  value={displayName}
                  onChange={(e) => setDisplayName(e.target.value)}
                />
              </div>
            </div>
            <div>
              <label className="block text-xs font-medium text-slate-400 mb-1.5">邀请码 <span className="text-slate-600">(选填)</span></label>
              <div className="relative">
                <Ticket size={16} className="absolute left-3 top-1/2 -translate-y-1/2 text-slate-500" />
                <input
                  className="input pl-9"
                  placeholder="如有邀请码请填入"
                  value={inviteCode}
                  onChange={(e) => setInviteCode(e.target.value)}
                />
              </div>
            </div>
            <button
              type="submit"
              className="w-full bg-gradient-to-r from-accent to-accent-glow text-white rounded-xl py-2.5 text-sm font-medium hover:shadow-glow transition-all disabled:opacity-50 disabled:cursor-not-allowed"
              disabled={loading || !username || !password}
            >
              {loading ? "注册中..." : "创 建 账 号"}
            </button>
          </form>

          <div className="mt-4 p-3 rounded-xl bg-amber-500/10 border border-amber-500/20 text-xs text-amber-200/80">
            ⚠️ 注册后账号为 <b className="text-amber-300">待审批</b> 状态, 管理员审批通过后方可登录交易。
          </div>

          <div className="text-center mt-4">
            <a
              href="/login"
              onClick={goLogin}
              className="text-sm text-accent-glow hover:text-amber-300 transition font-medium inline-flex items-center gap-1 cursor-pointer"
            >
              <ArrowLeft size={14} /> 返回登录
            </a>
          </div>
        </div>
      </div>
    </div>
  );
}