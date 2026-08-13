import { useState } from "react";
import { Users, Plus, KeyRound, Calendar, ShieldCheck, ShieldX, Lock, Bell, Trash2, Power, Key, UserCheck, Clock, XCircle, Eraser, UserCog, Copy, StickyNote, Radio, ArrowUpRight } from "lucide-react";
import { API } from "@/api/client";
import { useFetch } from "@/lib/useFetch";
import { useToast } from "@/components/ui/Toast";
import { Card, CardTitle, Badge, Button, Empty, Input, Select, Field } from "@/components/ui";
import { Modal } from "@/components/ui/Modal";
import { fmtTime } from "@/lib/utils";

export default function AdminCustomers() {
  const { data, reload } = useFetch(() => API.listCustomers(), []);
  const { push } = useToast();
  const [createModal, setCreateModal] = useState(false);
  const [custForm, setCustForm] = useState({ username: "", password: "", display_name: "", note: "" });
  const [authCid, setAuthCid] = useState<number | null>(null);
  const [auths, setAuths] = useState<any[]>([]);
  const [authForm, setAuthForm] = useState({ exchange: "all", starts_at: "", expires_at: "", note: "" });
  // 防共用设置
  const [guardCid, setGuardCid] = useState<number | null>(null);
  const [guardForm, setGuardForm] = useState({ single_exchange_multi_api_allowed: false, single_exchange_multi_api_limit: 2, multi_exchange_allowed: false, max_order_usdt: 5000 });
  // 告警配置
  const [alertCid, setAlertCid] = useState<number | null>(null);
  const [alertList, setAlertList] = useState<any[]>([]);
  const [alertModal, setAlertModal] = useState(false);
  // 重置密码
  const [pwdCid, setPwdCid] = useState<number | null>(null);
  const [deleteCid, setDeleteCid] = useState<number | null>(null);
  const [deleteName, setDeleteName] = useState("");
  const [deleteUsername, setDeleteUsername] = useState("");
  const [deleteConfirm, setDeleteConfirm] = useState("");
  const [pwdForm, setPwdForm] = useState({ new_password: "" });
  // 标签/备注编辑
  const [noteCid, setNoteCid] = useState<number | null>(null);
  const [noteCustName, setNoteCustName] = useState("");
  const [noteText, setNoteText] = useState("");
  // 客户类型修改确认
  const [typeChangeTarget, setTypeChangeTarget] = useState<{
    customer: any;
    nextType: "normal" | "internal";
  } | null>(null);
  const [typeChanging, setTypeChanging] = useState(false);
  // 一键清除数据(支持全部/按客户)
  const [resetModal, setResetModal] = useState(false);
  const [resetCid, setResetCid] = useState<number | null>(null);  // null=清除全部, 数字=按客户清除
  const [resetCustName, setResetCustName] = useState("");
  const [resetConfirm, setResetConfirm] = useState("");
  const [resetStrategy, setResetStrategy] = useState(true);
  const [resetting, setResetting] = useState(false);
  const [alertForm, setAlertForm] = useState<any>({
    name: "飞书告警", webhook_url: "", webhook_secret: "",
    enabled: true, on_signal: false, on_order: true, on_tp_sl: true,
    on_correct: true, on_risk: true, on_auth_expire: true, on_error: true,
  });

  const list: any[] = data || [];

  const activeCount = list.filter((c: any) => c.is_active).length;
  const authCount = list.filter((c: any) => c.authorized).length;
  const pendingCount = list.filter((c: any) => c.status === "pending").length;

  const createCust = async () => {
    try {
      await API.createCustomer(custForm);
      push("success", "客户已创建");
      setCreateModal(false);
      setCustForm({ username: "", password: "", display_name: "", note: "" });
      reload();
    } catch (e: any) {
      push("error", e?.response?.data?.message || "创建失败");
    }
  };

  const openAuth = async (cid: number) => {
    setAuthCid(cid);
    const r: any = await API.listAuthorizations(cid);
    setAuths(r || []);
    const now = new Date();
    const exp = new Date(now.getTime() + 30 * 86400000);
    setAuthForm({
      exchange: "all",
      starts_at: now.toISOString().slice(0, 16),
      expires_at: exp.toISOString().slice(0, 16),
      note: "",
    });
  };

  const grant = async () => {
    try {
      await API.grantAuth({
        customer_id: authCid,
        exchange: authForm.exchange,
        starts_at: new Date(authForm.starts_at).toISOString(),
        expires_at: new Date(authForm.expires_at).toISOString(),
        note: authForm.note,
        active: true,
      });
      push("success", "授权已授予");
      const r: any = await API.listAuthorizations(authCid!);
      setAuths(r || []);
      reload();
    } catch (e: any) {
      push("error", e?.response?.data?.message || "授权失败");
    }
  };

  const revoke = async (aid: number) => {
    try {
      await API.revokeAuth(aid);
      push("success", "已撤销授权");
      const r: any = await API.listAuthorizations(authCid!);
      setAuths(r || []);
      reload();
    } catch {}
  };

  const openGuard = (c: any) => {
    setGuardCid(c.id);
    setGuardForm({
      single_exchange_multi_api_allowed: !!c.single_exchange_multi_api_allowed,
      single_exchange_multi_api_limit: c.single_exchange_multi_api_limit ?? 2,
      multi_exchange_allowed: !!c.multi_exchange_allowed,
      max_order_usdt: c.max_order_usdt ?? 5000,
    });
  };

  const saveGuard = async () => {
    try {
      await API.updateCustomer(guardCid!, {
        single_exchange_multi_api_allowed: guardForm.single_exchange_multi_api_allowed,
        single_exchange_multi_api_limit: Number(guardForm.single_exchange_multi_api_limit || 1),
        multi_exchange_allowed: guardForm.multi_exchange_allowed,
        max_order_usdt: Number(guardForm.max_order_usdt),
      });
      push("success", "防共用设置已保存");
      setGuardCid(null);
      reload();
    } catch (e: any) {
      push("error", e?.response?.data?.message || "保存失败");
    }
  };

  // ===== 切换激活状态 =====
  const toggleActive = async (c: any) => {
    try {
      await API.updateCustomer(c.id, { is_active: !c.is_active });
      push("success", c.is_active ? "已停用" : "已激活");
      reload();
    } catch (e: any) {
      push("error", e?.response?.data?.message || "操作失败");
    }
  };

  const toggleSignalSummary = async (c: any) => {
    try {
      await API.updateCustomer(c.id, { show_signal_summary: !c.show_signal_summary });
      push("success", c.show_signal_summary ? "已隐藏信号汇总" : "已开放信号汇总");
      reload();
    } catch (e: any) {
      push("error", e?.response?.data?.message || "操作失败");
    }
  };

  // ===== 审批客户注册 =====
  const approveCustomer = async (c: any) => {
    try {
      await API.updateCustomer(c.id, { status: "active", is_active: true });
      push("success", `客户 ${c.username} 已审批通过`);
      reload();
    } catch (e: any) {
      push("error", e?.response?.data?.message || "审批失败");
    }
  };

  const rejectCustomer = async (c: any) => {
    if (!window.confirm(`确定拒绝客户 ${c.username} 的注册申请?`)) return;
    try {
      await API.updateCustomer(c.id, { status: "rejected", is_active: false });
      push("success", "已拒绝");
      reload();
    } catch (e: any) {
      push("error", e?.response?.data?.message || "操作失败");
    }
  };

  const customerTypeLabel = (type?: string) => type === "internal" ? "内部用户" : "普通客户";

  // ===== 设置客户类型(普通/内部)：二次确认，防误触 =====
  const openCustomerTypeChange = (c: any) => {
    const currentType = (c.customer_type || "normal") as "normal" | "internal";
    const nextType = currentType === "internal" ? "normal" : "internal";
    setTypeChangeTarget({ customer: c, nextType });
  };

  const confirmCustomerTypeChange = async () => {
    if (!typeChangeTarget) return;
    const { customer, nextType } = typeChangeTarget;
    if ((customer.customer_type || "normal") === nextType) {
      setTypeChangeTarget(null);
      return;
    }
    setTypeChanging(true);
    try {
      await API.setCustomerType(customer.id, nextType);
      push("success", `已设置为 ${customerTypeLabel(nextType)}`);
      setTypeChangeTarget(null);
      reload();
    } catch (e: any) {
      push("error", e?.response?.data?.message || "更新失败");
    } finally {
      setTypeChanging(false);
    }
  };

  // ===== 复制邀请码 / 邀请链接 =====
  const copyInviteCode = async (code: string) => {
    try {
      await navigator.clipboard?.writeText(code);
      push("success", `邀请码 ${code} 已复制`);
    } catch {
      push("error", "复制失败,请手动复制");
    }
  };

  const copyInviteLink = async (c: any) => {
    try {
      const res: any = await API.getInviteLink(c.id);
      const path = typeof res === "string" ? res : res?.invite_link || res?.link || "";
      if (!path) {
        push("error", "未获取到邀请链接");
        return;
      }
      // 构建完整 URL
      const fullUrl = path.startsWith("http") ? path : `${window.location.origin}${path}`;
      await navigator.clipboard?.writeText(fullUrl);
      push("success", `邀请链接已复制: ${fullUrl}`);
    } catch (e: any) {
      push("error", e?.response?.data?.message || "获取邀请链接失败");
    }
  };

  // ===== 管理员以客户身份进入客户页面 =====
  const loginAsCustomer = async (c: any) => {
    try {
      const res: any = await API.loginAsCustomer(c.id);
      // S9修复: 不再向新窗口localStorage写入token,改用URL hash传递一次性impersonate token
      const newWin = window.open(`/#impersonate_token=${encodeURIComponent(res.access_token)}`, "_blank");
      if (newWin) {
        push("success", `已以 ${res.display_name || res.username} 身份打开新窗口`);
      } else {
        push("error", "新窗口被浏览器拦截,请允许弹窗");
      }
    } catch (e: any) {
      push("error", e?.response?.data?.detail || "操作失败");
    }
  };

  // ===== 告警管理 =====
  const openAlerts = async (cid: number) => {
    setAlertCid(cid);
    setAlertModal(false);
    try {
      const r: any = await API.listCustomerAlerts(cid);
      setAlertList(r || []);
    } catch {
      setAlertList([]);
    }
  };

  const saveAlert = async () => {
    try {
      await API.createCustomerAlert(alertCid!, alertForm);
      push("success", "告警配置已添加");
      const r: any = await API.listCustomerAlerts(alertCid!);
      setAlertList(r || []);
      setAlertModal(false);
      setAlertForm({
        name: "飞书告警", webhook_url: "", webhook_secret: "",
        enabled: true, on_signal: false, on_order: true, on_tp_sl: true,
        on_correct: true, on_risk: true, on_auth_expire: true, on_error: true,
      });
    } catch (e: any) {
      push("error", e?.response?.data?.message || "添加失败");
    }
  };

  const toggleAlert = async (aid: number, enabled: boolean) => {
    try {
      await API.updateCustomerAlert(alertCid!, aid, { enabled: !enabled });
      push("success", !enabled ? "已启用" : "已停用");
      const r: any = await API.listCustomerAlerts(alertCid!);
      setAlertList(r || []);
    } catch (e: any) {
      push("error", e?.response?.data?.message || "操作失败");
    }
  };

  const removeAlert = async (aid: number) => {
    if (!window.confirm("确认删除此告警配置?")) return;
    try {
      await API.deleteCustomerAlert(alertCid!, aid);
      push("success", "已删除");
      const r: any = await API.listCustomerAlerts(alertCid!);
      setAlertList(r || []);
    } catch (e: any) {
      push("error", e?.response?.data?.message || "删除失败");
    }
  };


  // ===== 重置客户密码 =====
  const openResetPwd = (cid: number) => {
    setPwdCid(cid);
    setPwdForm({ new_password: "" });
  };

  // ===== 编辑标签/备注 =====
  const openNote = (c: any) => {
    setNoteCid(c.id);
    setNoteCustName(c.display_name || c.username);
    setNoteText(c.note || "");
  };

  const saveNote = async () => {
    try {
      await API.updateCustomer(noteCid!, { note: noteText });
      push("success", "备注已保存");
      setNoteCid(null);
      setNoteCustName("");
      setNoteText("");
      reload();
    } catch (e: any) {
      push("error", e?.response?.data?.message || "保存失败");
    }
  };

  const saveResetPwd = async () => {
    if (!pwdForm.new_password) {
      push("error", "请输入新密码");
      return;
    }
    if (pwdForm.new_password.length < 8) {
      push("error", "密码至少 8 位");
      return;
    }
    if (!/[a-zA-Z]/.test(pwdForm.new_password)) {
      push("error", "密码必须包含至少一个字母");
      return;
    }
    if (!/\d/.test(pwdForm.new_password)) {
      push("error", "密码必须包含至少一个数字");
      return;
    }
    try {
      await API.resetCustomerPassword(pwdCid!, pwdForm.new_password);
      push("success", "密码已重置");
      setPwdCid(null);
      setPwdForm({ new_password: "" });
    } catch (e: any) {
      push("error", e?.response?.data?.detail || e?.response?.data?.message || "重置失败");
    }
  };

  const confirmDelete = async () => {
    if (!deleteCid) return;
    if (deleteConfirm.trim() !== deleteUsername) {
      push("error", `确认文字不匹配,请输入用户名: ${deleteUsername}`);
      return;
    }
    try {
      await API.deleteCustomer(deleteCid, deleteConfirm.trim());
      push("success", `客户 ${deleteName} 已删除`);
      setDeleteCid(null);
      setDeleteName("");
      setDeleteUsername("");
      setDeleteConfirm("");
      reload();
    } catch (e: any) {
      push("error", e?.response?.data?.detail || e?.response?.data?.message || "删除失败");
    }
  };

  // ===== 一键清除数据(支持全部/按客户) =====
  const expectedResetText = resetCid !== null ? `确认清除 ${resetCustName} 的数据` : "确认清除所有测试数据";

  const openResetAll = () => {
    setResetCid(null);
    setResetCustName("");
    setResetConfirm("");
    setResetStrategy(true);
    setResetModal(true);
  };

  const openResetCust = (c: any) => {
    setResetCid(c.id);
    setResetCustName(c.display_name || c.username);
    setResetConfirm("");
    setResetStrategy(true);
    setResetModal(true);
  };

  const doResetData = async () => {
    if (resetConfirm !== expectedResetText) {
      push("error", `确认文字不匹配,请输入: ${expectedResetText}`);
      return;
    }
    setResetting(true);
    try {
      const res: any = await API.resetData(resetConfirm, resetStrategy, resetCid);
      const total = res?.total_deleted ?? 0;
      const scope = res?.scope ?? (resetCid !== null ? `客户 ${resetCustName}` : "所有客户");
      push("success", `已清除 ${scope} 的数据 (共 ${total} 条记录)`);
      setResetModal(false);
      setResetConfirm("");
      setResetCid(null);
      setResetCustName("");
      setResetStrategy(true);
      reload();
    } catch (e: any) {
      push("error", e?.response?.data?.detail || e?.response?.data?.message || "清除失败");
    } finally {
      setResetting(false);
    }
  };

  return (
    <div className="space-y-6">
      <div className="flex items-center justify-between">
        <div>
          <h1 className="text-xl font-bold gradient-text flex items-center gap-2">客户管理</h1>
          <p className="text-sm text-slate-500 mt-1">管理客户账号、时间授权与防共用限制</p>
        </div>
        <div className="flex items-center gap-2">
          <Button variant="danger" onClick={openResetAll}>
            <Eraser size={15} /> 清除全部数据
          </Button>
          <Button onClick={() => setCreateModal(true)}><Plus size={15} /> 新建客户</Button>
        </div>
      </div>

      {/* KPI 概览 */}
      <div className="grid grid-cols-2 md:grid-cols-5 gap-3">
        <div className="glass p-4"><div className="text-xs text-slate-500 mb-1">客户总数</div><div className="text-xl md:text-2xl font-bold font-mono text-slate-100">{list.length}</div></div>
        <div className="glass p-4"><div className="text-xs text-slate-500 mb-1">已激活</div><div className="text-xl md:text-2xl font-bold font-mono text-profit">{activeCount}</div></div>
        <div className="glass p-4"><div className="text-xs text-slate-500 mb-1">待审批</div><div className="text-xl md:text-2xl font-bold font-mono text-warn">{pendingCount}</div></div>
        <div className="glass p-4"><div className="text-xs text-slate-500 mb-1">已授权</div><div className="text-xl md:text-2xl font-bold font-mono text-accent-glow">{authCount}</div></div>
        <div className="glass p-4"><div className="text-xs text-slate-500 mb-1">未授权</div><div className="text-xl md:text-2xl font-bold font-mono text-loss">{list.length - authCount}</div></div>
      </div>

      <Card>
        {list.length === 0 ? (
          <Empty text="暂无客户" />
        ) : (
          <>
          {/* 桌面端表格 */}
          <div className="hidden md:block overflow-x-auto">
            <table className="w-full text-sm">
              <thead>
                <tr className="text-xs text-slate-500 border-b border-border">
                  <th className="text-left py-3 px-3">ID</th>
                  <th className="text-left px-3">用户名</th>
                  <th className="text-left px-3">显示名</th>
                  <th className="text-center px-3">类型</th>
                  <th className="text-center px-3">授权状态</th>
                  <th className="text-center px-3">审批</th>
                  <th className="text-left px-3">到期时间</th>
                  <th className="text-center px-3">多开</th>
                  <th className="text-center px-3">单笔上限</th>
                  <th className="text-center px-3">信号汇总</th>
                  <th className="text-center px-3">激活</th>
                  <th className="text-center px-3">邀请码</th>
                  <th className="text-left px-3">邀请人</th>
                  <th className="text-left px-3">备注</th>
                  <th className="text-center px-3">操作</th>
                </tr>
              </thead>
              <tbody>
                {list.map((c) => (
                  <tr key={c.id} className="border-b border-border/50 hover:bg-bg-hover/40">
                    <td className="py-3 px-3 text-slate-500">{c.id}</td>
                    <td className={`px-3 font-medium ${c.customer_type === "internal" ? "text-amber-400" : "text-slate-100"}`}>{c.username}</td>
                    <td className="px-3 text-slate-300">{c.display_name || "—"}</td>
                    <td className="px-3 text-center">
                      <Badge tone={c.customer_type === "internal" ? "gold" : "default"}>
                        {c.customer_type === "internal" ? "内部用户" : "普通客户"}
                      </Badge>
                    </td>
                    <td className="px-3 text-center">
                      {c.authorized ? (
                        <Badge tone="profit"><ShieldCheck size={11} /> 已授权</Badge>
                      ) : (
                        <Badge tone="loss"><ShieldX size={11} /> 未授权</Badge>
                      )}
                    </td>
                    <td className="px-3 text-center">
                      {c.status === "pending" ? (
                        <Badge tone="warn"><Clock size={11} /> 待审批</Badge>
                      ) : c.status === "active" ? (
                        <Badge tone="profit"><UserCheck size={11} /> 已通过</Badge>
                      ) : (
                        <Badge tone="loss"><XCircle size={11} /> 已拒绝</Badge>
                      )}
                    </td>
                    <td className="px-3 text-xs text-slate-400">{fmtTime(c.auth_expires_at)}</td>
                    <td className="px-3 text-center">
                      <Badge tone={c.multi_exchange_allowed ? "profit" : "default"}>
                        {c.multi_exchange_allowed ? "允许" : "禁止"}
                      </Badge>
                    </td>
                    <td className="px-3 text-center text-xs text-slate-300">
                      {(c.max_order_usdt ?? 0).toLocaleString()} U
                    </td>
                    <td className="px-3 text-center">
                      <Badge tone={c.show_signal_summary ? "profit" : "default"}>
                        {c.show_signal_summary ? "开放" : "隐藏"}
                      </Badge>
                    </td>
                    <td className="px-3 text-center">
                      <Badge tone={c.is_active ? "profit" : "default"}>{c.is_active ? "激活" : "停用"}</Badge>
                    </td>
                    <td className="px-3 text-center">
                      {c.invite_code ? (
                        <button
                          title="点击复制邀请码"
                          onClick={() => copyInviteCode(c.invite_code)}
                          className="inline-flex items-center gap-1 font-mono text-xs text-accent-glow hover:underline"
                        >
                          <Copy size={11} /> {c.invite_code}
                        </button>
                      ) : (
                        <span className="text-slate-600">—</span>
                      )}
                    </td>
                    <td className="px-3 text-slate-300 text-xs">{c.inviter_name || "—"}</td>
                    <td className="px-3 text-xs text-slate-400 max-w-[120px] truncate" title={c.note || ""}>{c.note || "—"}</td>
                    <td className="px-3 text-center">
                      <div className="flex gap-1.5 justify-center items-center">
                        <button
                          title={`以 ${c.display_name || c.username} 身份进入客户页面`}
                          onClick={() => loginAsCustomer(c)}
                          className="inline-flex items-center gap-1 rounded-lg border border-emerald/30 bg-emerald/10 px-2 py-1 text-xs text-emerald hover:bg-emerald/15 transition"
                        >
                          <ArrowUpRight size={12} />
                          进入
                        </button>
                        <button
                          title={`修改客户类型：当前为 ${customerTypeLabel(c.customer_type)}`}
                          onClick={() => openCustomerTypeChange(c)}
                          className="inline-flex items-center gap-1 rounded-lg border border-border/60 bg-bg-soft px-2 py-1 text-xs text-slate-200 hover:border-gold/40 hover:text-gold transition"
                        >
                          <UserCog size={12} />
                          修改类型
                        </button>
                        {c.status === "pending" && (
                          <>
                            <button title="审批通过" className="action-icon text-profit" onClick={() => approveCustomer(c)}>
                              <UserCheck size={14} />
                            </button>
                            <button title="拒绝" className="action-icon text-loss" onClick={() => rejectCustomer(c)}>
                              <XCircle size={14} />
                            </button>
                          </>
                        )}
                        <button title={c.is_active ? "停用" : "激活"} className={`action-icon ${c.is_active ? "text-profit" : "text-warn"}`} onClick={() => toggleActive(c)}>
                          <Power size={14} />
                        </button>
                        <button title="授权" className="action-icon" onClick={() => openAuth(c.id)}>
                          <KeyRound size={14} />
                        </button>
                        <button title="防共用" className="action-icon" onClick={() => openGuard(c)}>
                          <Lock size={14} />
                        </button>
                        <button
                          title={c.show_signal_summary ? "隐藏信号汇总" : "开放信号汇总"}
                          className={`action-icon ${c.show_signal_summary ? "text-accent-glow" : ""}`}
                          onClick={() => toggleSignalSummary(c)}
                        >
                          <Radio size={14} />
                        </button>
                        <button title="通知" className="action-icon" onClick={() => openAlerts(c.id)}>
                          <Bell size={14} />
                        </button>
                        <button title="邀请链接" className="action-icon" onClick={() => copyInviteLink(c)}>
                          <UserCog size={14} />
                        </button>
                        <button title="编辑备注" className={`action-icon ${c.note ? "text-accent-glow" : ""}`} onClick={() => openNote(c)}>
                          <StickyNote size={14} />
                        </button>
                        <button title="密码" className="action-icon" onClick={() => openResetPwd(c.id)}>
                          <Key size={14} />
                        </button>
                        <button title="清除该客户数据" className="action-icon text-warn" onClick={() => openResetCust(c)}>
                          <Eraser size={14} />
                        </button>
                        <button
                          title="删除客户"
                          className="action-icon action-icon-danger"
                          onClick={() => {
                            setDeleteCid(c.id);
                            setDeleteName(c.display_name || c.username);
                            setDeleteUsername(c.username);
                            setDeleteConfirm("");
                          }}
                        >
                          <Trash2 size={14} />
                        </button>
                      </div>
                    </td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>

          {/* 移动端卡片列表 */}
          <div className="md:hidden space-y-3">
            {list.map((c) => (
              <div key={c.id} className="glass-soft p-4 glow-border">
                <div className="flex items-center justify-between mb-3">
                  <div className="flex items-center gap-2 min-w-0">
                    <span className="text-slate-500 text-xs font-mono">#{c.id}</span>
                    <span className={`font-semibold truncate ${c.customer_type === "internal" ? "text-amber-400" : "text-slate-100"}`}>{c.username}</span>
                  </div>
                  <div className="flex items-center gap-1.5 shrink-0">
                    <Badge tone={c.customer_type === "internal" ? "gold" : "default"}>
                      {c.customer_type === "internal" ? "内部" : "普通"}
                    </Badge>
                    {c.status === "pending" && <Badge tone="warn"><Clock size={11} /> 待审批</Badge>}
                    {c.status === "active" && <Badge tone="profit"><UserCheck size={11} /> 已通过</Badge>}
                    {c.status === "rejected" && <Badge tone="loss"><XCircle size={11} /> 已拒绝</Badge>}
                    {c.authorized ? (
                      <Badge tone="profit"><ShieldCheck size={11} /> 已授权</Badge>
                    ) : (
                      <Badge tone="loss"><ShieldX size={11} /> 未授权</Badge>
                    )}
                  </div>
                </div>
                <div className="grid grid-cols-2 gap-2 text-xs mb-3">
                  <div>
                    <span className="text-slate-500">显示名</span>
                    <div className="text-slate-200">{c.display_name || "—"}</div>
                  </div>
                  <div>
                    <span className="text-slate-500">到期时间</span>
                    <div className="text-slate-300">{fmtTime(c.auth_expires_at)}</div>
                  </div>
                  <div>
                    <span className="text-slate-500">多开</span>
                    <div>{c.multi_exchange_allowed ? <Badge tone="profit">允许</Badge> : <Badge tone="default">禁止</Badge>}</div>
                  </div>
                  <div>
                    <span className="text-slate-500">单笔上限</span>
                    <div className="text-slate-200">{(c.max_order_usdt ?? 0).toLocaleString()} U</div>
                  </div>
                  <div>
                    <span className="text-slate-500">信号汇总</span>
                    <div>
                      <Badge tone={c.show_signal_summary ? "profit" : "default"}>
                        {c.show_signal_summary ? "开放" : "隐藏"}
                      </Badge>
                    </div>
                  </div>
                  <div>
                    <span className="text-slate-500">邀请码</span>
                    <div>
                      {c.invite_code ? (
                        <button
                          onClick={() => copyInviteCode(c.invite_code)}
                          className="inline-flex items-center gap-1 font-mono text-accent-glow hover:underline"
                        >
                          <Copy size={11} /> {c.invite_code}
                        </button>
                      ) : (
                        <span className="text-slate-600">—</span>
                      )}
                    </div>
                  </div>
                  <div>
                    <span className="text-slate-500">邀请人</span>
                    <div className="text-slate-200 truncate">{c.inviter_name || "—"}</div>
                  </div>
                  <div className="col-span-2">
                    <span className="text-slate-500">备注</span>
                    <div className="text-slate-300 text-xs truncate" title={c.note || ""}>{c.note || "—"}</div>
                  </div>
                </div>
                <div className="flex items-center justify-between pt-3 border-t border-border/40 gap-2">
                  <div className="flex items-center gap-2">
                    <Badge tone={c.is_active ? "profit" : "default"}>{c.is_active ? "激活" : "停用"}</Badge>
                    <button
                      title={`以 ${c.display_name || c.username} 身份进入客户页面`}
                      onClick={() => loginAsCustomer(c)}
                      className="inline-flex items-center gap-1 rounded-lg border border-emerald/30 bg-emerald/10 px-2 py-1 text-xs text-emerald hover:bg-emerald/15 transition"
                    >
                      <ArrowUpRight size={12} />
                      进入
                    </button>
                    <button
                      title={`修改客户类型：当前为 ${customerTypeLabel(c.customer_type)}`}
                      onClick={() => openCustomerTypeChange(c)}
                      className="inline-flex items-center gap-1 rounded-lg border border-border/60 bg-bg-soft px-2 py-1 text-xs text-slate-200 hover:border-gold/40 hover:text-gold transition"
                    >
                      <UserCog size={12} />
                      修改类型
                    </button>
                  </div>
                  <div className="flex gap-1.5">
                    {c.status === "pending" && (
                      <>
                        <button title="审批通过" className="action-icon text-profit" onClick={() => approveCustomer(c)}>
                          <UserCheck size={15} />
                        </button>
                        <button title="拒绝" className="action-icon text-loss" onClick={() => rejectCustomer(c)}>
                          <XCircle size={15} />
                        </button>
                      </>
                    )}
                    <button title={c.is_active ? "停用" : "激活"} className={`action-icon ${c.is_active ? "text-profit" : "text-warn"}`} onClick={() => toggleActive(c)}>
                      <Power size={15} />
                    </button>
                    <button title="授权" className="action-icon" onClick={() => openAuth(c.id)}>
                      <KeyRound size={15} />
                    </button>
                    <button title="防共用" className="action-icon" onClick={() => openGuard(c)}>
                      <Lock size={15} />
                    </button>
                    <button
                      title={c.show_signal_summary ? "隐藏信号汇总" : "开放信号汇总"}
                      className={`action-icon ${c.show_signal_summary ? "text-accent-glow" : ""}`}
                      onClick={() => toggleSignalSummary(c)}
                    >
                      <Radio size={15} />
                    </button>
                    <button title="通知" className="action-icon" onClick={() => openAlerts(c.id)}>
                      <Bell size={15} />
                    </button>
                    <button title="邀请链接" className="action-icon" onClick={() => copyInviteLink(c)}>
                      <UserCog size={15} />
                    </button>
                    <button title="编辑备注" className={`action-icon ${c.note ? "text-accent-glow" : ""}`} onClick={() => openNote(c)}>
                      <StickyNote size={15} />
                    </button>
                    <button title="密码" className="action-icon" onClick={() => openResetPwd(c.id)}>
                      <Key size={15} />
                    </button>
                    <button title="清除该客户数据" className="action-icon text-warn" onClick={() => openResetCust(c)}>
                      <Eraser size={15} />
                    </button>
                    <button
                      title="删除客户"
                      className="action-icon action-icon-danger"
                      onClick={() => {
                        setDeleteCid(c.id);
                        setDeleteName(c.display_name || c.username);
                        setDeleteUsername(c.username);
                        setDeleteConfirm("");
                      }}
                    >
                      <Trash2 size={15} />
                    </button>
                  </div>
                </div>
              </div>
            ))}
          </div>
          </>
        )}
      </Card>

      <Modal open={createModal} onClose={() => setCreateModal(false)} title="新建客户">
        <div className="space-y-4">
          <div className="grid grid-cols-2 gap-3">
            <Field label="用户名"><Input value={custForm.username} onChange={(e) => setCustForm({ ...custForm, username: e.target.value })} /></Field>
            <Field label="初始密码"><Input value={custForm.password} onChange={(e) => setCustForm({ ...custForm, password: e.target.value })} /></Field>
          </div>
          <Field label="显示名"><Input value={custForm.display_name} onChange={(e) => setCustForm({ ...custForm, display_name: e.target.value })} /></Field>
          <Field label="备注"><Input value={custForm.note} onChange={(e) => setCustForm({ ...custForm, note: e.target.value })} /></Field>
          <Button className="w-full" onClick={createCust}>创建客户</Button>
        </div>
      </Modal>

      {/* 客户类型修改确认 Modal */}
      <Modal
        open={typeChangeTarget !== null}
        onClose={() => { if (!typeChanging) setTypeChangeTarget(null); }}
        title="确认修改客户类型"
        width="max-w-lg"
      >
        {typeChangeTarget && (
          <div className="space-y-4">
            <div className="glass-soft p-4 rounded-lg border border-gold/20">
              <div className="flex items-start gap-3">
                <div className="stat-icon-wrap shrink-0">
                  <UserCog size={17} className="text-gold" />
                </div>
                <div className="min-w-0">
                  <div className="text-sm font-semibold text-slate-100">
                    {typeChangeTarget.customer.display_name || typeChangeTarget.customer.username}
                  </div>
                  <div className="text-xs text-slate-500 mt-1">
                    用户名：{typeChangeTarget.customer.username} · ID：{typeChangeTarget.customer.id}
                  </div>
                </div>
              </div>

              <div className="mt-4 grid grid-cols-[1fr_auto_1fr] items-center gap-3 text-center">
                <div className="rounded-xl border border-border/60 bg-bg-soft p-3">
                  <div className="text-[11px] text-slate-500 mb-1">当前类型</div>
                  <Badge tone={typeChangeTarget.customer.customer_type === "internal" ? "gold" : "default"}>
                    {customerTypeLabel(typeChangeTarget.customer.customer_type)}
                  </Badge>
                </div>
                <ArrowUpRight size={16} className="text-slate-500" />
                <div className="rounded-xl border border-gold/30 bg-gold/5 p-3">
                  <div className="text-[11px] text-slate-500 mb-1">修改为</div>
                  <Badge tone={typeChangeTarget.nextType === "internal" ? "gold" : "default"}>
                    {customerTypeLabel(typeChangeTarget.nextType)}
                  </Badge>
                </div>
              </div>
            </div>

            <div className="text-xs text-slate-400 leading-relaxed p-3 rounded-lg bg-warn/5 border border-warn/20">
              客户类型会影响后台统计、内部账号识别与后续管理口径。为避免误触，只有点击下方确认按钮后才会真正修改。
            </div>

            <div className="flex gap-2">
              <Button
                variant="ghost"
                className="flex-1"
                disabled={typeChanging}
                onClick={() => setTypeChangeTarget(null)}
              >
                取消
              </Button>
              <Button
                variant={typeChangeTarget.nextType === "internal" ? "gold" : "primary"}
                className="flex-1"
                disabled={typeChanging}
                onClick={confirmCustomerTypeChange}
              >
                {typeChanging ? "修改中..." : `确认修改为${customerTypeLabel(typeChangeTarget.nextType)}`}
              </Button>
            </div>
          </div>
        )}
      </Modal>

      <Modal open={authCid !== null} onClose={() => setAuthCid(null)} title={`客户 #${authCid} 时间授权`} width="max-w-2xl">
        <div className="space-y-4">
          <div className="grid grid-cols-3 gap-3">
            <Field label="交易所">
              <Select value={authForm.exchange} onChange={(e) => setAuthForm({ ...authForm, exchange: e.target.value })}>
                <option value="all">全部交易所</option>
                <option value="okx">OKX</option>
                <option value="binance">Binance</option>
                <option value="bybit">Bybit</option>
              </Select>
            </Field>
            <Field label="开始时间"><Input type="datetime-local" value={authForm.starts_at} onChange={(e) => setAuthForm({ ...authForm, starts_at: e.target.value })} /></Field>
            <Field label="到期时间"><Input type="datetime-local" value={authForm.expires_at} onChange={(e) => setAuthForm({ ...authForm, expires_at: e.target.value })} /></Field>
          </div>
          <Field label="备注"><Input value={authForm.note} onChange={(e) => setAuthForm({ ...authForm, note: e.target.value })} /></Field>
          <Button onClick={grant}><Calendar size={15} /> 授予授权</Button>

          <div>
            <div className="text-xs text-slate-400 mb-2">已有授权</div>
            {auths.length === 0 ? (
              <Empty text="暂无授权记录" />
            ) : (
              <div className="space-y-2">
                {auths.map((a) => (
                  <div key={a.id} className="glass-soft p-3 flex items-center gap-3">
                    <Badge tone="accent">{a.exchange}</Badge>
                    <div className="text-xs text-slate-300 flex-1">
                      {fmtTime(a.starts_at)} → {fmtTime(a.expires_at)}
                    </div>
                    <Badge tone={a.active ? "profit" : "default"}>{a.active ? "有效" : "已撤销"}</Badge>
                    {a.active && (
                      <Button variant="danger" className="px-2 py-1 text-xs" onClick={() => revoke(a.id)}>撤销</Button>
                    )}
                  </div>
                ))}
              </div>
            )}
          </div>
        </div>
      </Modal>

      <Modal open={guardCid !== null} onClose={() => setGuardCid(null)} title={`客户 #${guardCid} 防共用设置`}>
        <div className="space-y-4">
          <div className="text-xs text-slate-400 p-3 glass-soft rounded">
            默认每个账号只能绑定 <b>1 个交易所 1 个 API</b>。<br />
            勾选「允许多交易所」后,该客户可绑定多个交易所(多开,需付费授权)。<br />
            单笔下单上限为管理员强制下发,客户无法修改,优先级高于客户自配。
          </div>
          <label className="flex items-center gap-2 text-sm text-slate-200">
            <input
              type="checkbox"
              className="accent-accent"
              checked={guardForm.single_exchange_multi_api_allowed}
              onChange={(e) => setGuardForm({ ...guardForm, single_exchange_multi_api_allowed: e.target.checked })}
            />
            允许单交易所多 API
          </label>
          <Field label="单交易所同模式允许 API 数量">
            <Input
              type="number"
              min={1}
              max={20}
              value={guardForm.single_exchange_multi_api_limit}
              onChange={(e) => setGuardForm({ ...guardForm, single_exchange_multi_api_limit: Number(e.target.value) })}
              disabled={!guardForm.single_exchange_multi_api_allowed && !guardForm.multi_exchange_allowed}
            />
          </Field>
          <label className="flex items-center gap-2 text-sm text-slate-200">
            <input
              type="checkbox"
              className="accent-accent"
              checked={guardForm.multi_exchange_allowed}
              onChange={(e) => setGuardForm({ ...guardForm, multi_exchange_allowed: e.target.checked })}
            />
            允许多交易所(多开授权)
          </label>
          <Field label="单笔下单上限 (USDT, 0=不限)">
            <Input
              type="number"
              value={guardForm.max_order_usdt}
              onChange={(e) => setGuardForm({ ...guardForm, max_order_usdt: Number(e.target.value) })}
            />
          </Field>
          <Button className="w-full" onClick={saveGuard}>保存防共用设置</Button>
        </div>
      </Modal>

      {/* 告警配置列表 Modal */}
      <Modal open={alertCid !== null && !alertModal} onClose={() => setAlertCid(null)} title={`客户 #${alertCid} 通知告警配置`} width="max-w-2xl">
        <div className="space-y-3">
          <div className="text-xs text-slate-400 p-3 glass-soft rounded">
            管理员可在此为客户配置飞书 Webhook 告警。<br />
            告警事件分为三类：<b>信号通知</b>(收到信号/信号纠错)、<b>交易通知</b>(下单成交/止盈止损)、<b>风控通知</b>(风控熔断/系统错误/授权到期)。<br />
            客户也可在客户端设置页面自行配置。
          </div>
          {alertList.length === 0 ? (
            <Empty text="该客户尚未配置告警" />
          ) : (
            <div className="space-y-2">
              {alertList.map((a: any) => (
                <div key={a.id} className="glass-soft p-3 rounded-lg">
                  <div className="flex items-center gap-3 mb-2">
                    <Bell size={16} className="text-accent-glow" />
                    <div className="flex-1 min-w-0">
                      <div className="text-sm font-medium text-slate-100">{a.name}</div>
                      <div className="text-xs text-slate-500 font-mono truncate">{a.webhook_url}</div>
                    </div>
                    <button
                      onClick={() => toggleAlert(a.id, a.enabled)}
                      className={`px-2 py-1 rounded text-xs flex items-center gap-1 ${a.enabled ? "bg-profit/15 text-profit" : "bg-slate-600/30 text-slate-400"}`}
                    >
                      <Power size={12} />
                      {a.enabled ? "启用" : "停用"}
                    </button>
                    <button onClick={() => removeAlert(a.id)} className="text-slate-400 hover:text-loss p-1">
                      <Trash2 size={14} />
                    </button>
                  </div>
                  <div className="flex flex-wrap gap-1.5">
                    {a.on_signal && <span className="text-xs px-1.5 py-0.5 rounded bg-blue-500/15 text-blue-400">收到信号</span>}
                    {a.on_order && <span className="text-xs px-1.5 py-0.5 rounded bg-green-500/15 text-green-400">下单成交</span>}
                    {a.on_tp_sl && <span className="text-xs px-1.5 py-0.5 rounded bg-green-500/15 text-green-400">止盈止损</span>}
                    {a.on_correct && <span className="text-xs px-1.5 py-0.5 rounded bg-blue-500/15 text-blue-400">信号纠错</span>}
                    {a.on_risk && <span className="text-xs px-1.5 py-0.5 rounded bg-red-500/15 text-red-400">风控熔断</span>}
                    {a.on_error && <span className="text-xs px-1.5 py-0.5 rounded bg-red-500/15 text-red-400">系统错误</span>}
                    {a.on_auth_expire && <span className="text-xs px-1.5 py-0.5 rounded bg-orange-500/15 text-orange-400">授权到期</span>}
                  </div>
                </div>
              ))}
            </div>
          )}
          <Button
            className="w-full"
            onClick={() => {
              setAlertForm({
                name: "飞书告警", webhook_url: "", webhook_secret: "",
                enabled: true, on_signal: false, on_order: true, on_tp_sl: true,
                on_correct: true, on_risk: true, on_auth_expire: true, on_error: true,
              });
              setAlertModal(true);
            }}
          >
            <Plus size={15} /> 添加告警配置
          </Button>
        </div>
      </Modal>

      {/* 添加告警配置 Modal */}
      <Modal open={alertModal} onClose={() => setAlertModal(false)} title={`为客户 #${alertCid} 添加告警`} width="max-w-lg">
        <div className="space-y-4">
          <Field label="告警名称">
            <Input value={alertForm.name} onChange={(e) => setAlertForm({ ...alertForm, name: e.target.value })} />
          </Field>
          <Field label="飞书 Webhook URL">
            <Input
              value={alertForm.webhook_url}
              onChange={(e) => setAlertForm({ ...alertForm, webhook_url: e.target.value })}
              placeholder="https://open.feishu.cn/open-apis/bot/v2/hook/xxx"
            />
          </Field>
          <Field label="签名校验密钥(可选)">
            <Input
              type="password"
              value={alertForm.webhook_secret}
              onChange={(e) => setAlertForm({ ...alertForm, webhook_secret: e.target.value })}
              placeholder="飞书机器人安全设置中开启签名校验后填入"
            />
          </Field>
          <div>
            <div className="text-xs text-slate-400 mb-2">通知事件(勾选后该事件会推送到飞书)</div>
            <div className="space-y-2">
              <div className="text-xs text-blue-400 font-medium">信号通知</div>
              <div className="grid grid-cols-2 gap-2 pl-3">
                <label className="flex items-center gap-2 text-sm text-slate-200">
                  <input type="checkbox" className="accent-accent" checked={alertForm.on_signal}
                    onChange={(e) => setAlertForm({ ...alertForm, on_signal: e.target.checked })} />
                  收到信号
                </label>
                <label className="flex items-center gap-2 text-sm text-slate-200">
                  <input type="checkbox" className="accent-accent" checked={alertForm.on_correct}
                    onChange={(e) => setAlertForm({ ...alertForm, on_correct: e.target.checked })} />
                  信号纠错
                </label>
              </div>
              <div className="text-xs text-green-400 font-medium mt-2">交易通知</div>
              <div className="grid grid-cols-2 gap-2 pl-3">
                <label className="flex items-center gap-2 text-sm text-slate-200">
                  <input type="checkbox" className="accent-accent" checked={alertForm.on_order}
                    onChange={(e) => setAlertForm({ ...alertForm, on_order: e.target.checked })} />
                  下单成交
                </label>
                <label className="flex items-center gap-2 text-sm text-slate-200">
                  <input type="checkbox" className="accent-accent" checked={alertForm.on_tp_sl}
                    onChange={(e) => setAlertForm({ ...alertForm, on_tp_sl: e.target.checked })} />
                  止盈止损
                </label>
              </div>
              <div className="text-xs text-red-400 font-medium mt-2">风控通知</div>
              <div className="grid grid-cols-2 gap-2 pl-3">
                <label className="flex items-center gap-2 text-sm text-slate-200">
                  <input type="checkbox" className="accent-accent" checked={alertForm.on_risk}
                    onChange={(e) => setAlertForm({ ...alertForm, on_risk: e.target.checked })} />
                  风控熔断
                </label>
                <label className="flex items-center gap-2 text-sm text-slate-200">
                  <input type="checkbox" className="accent-accent" checked={alertForm.on_error}
                    onChange={(e) => setAlertForm({ ...alertForm, on_error: e.target.checked })} />
                  系统错误
                </label>
                <label className="flex items-center gap-2 text-sm text-slate-200">
                  <input type="checkbox" className="accent-accent" checked={alertForm.on_auth_expire}
                    onChange={(e) => setAlertForm({ ...alertForm, on_auth_expire: e.target.checked })} />
                  授权到期
                </label>
              </div>
            </div>
          </div>
          <Button className="w-full" onClick={saveAlert}>保存告警配置</Button>
        </div>
      </Modal>

      {/* 重置密码 Modal */}
      <Modal open={pwdCid !== null} onClose={() => setPwdCid(null)} title={`客户 #${pwdCid} 重置密码`}>
        <div className="space-y-4">
          <div className="text-xs text-slate-400 p-3 glass-soft rounded">
            重置后客户需要使用新密码登录。密码至少 8 位，并且必须包含字母和数字。
          </div>
          <Field label="新密码">
            <Input
              type="password"
              value={pwdForm.new_password}
              onChange={(e) => setPwdForm({ new_password: e.target.value })}
              placeholder="输入新密码 (至少 8 位，含字母和数字)"
              autoFocus
            />
          </Field>
          <Button className="w-full" onClick={saveResetPwd}>确认重置</Button>
        </div>
      </Modal>

      {/* 删除客户确认 Modal */}
      <Modal
        open={deleteCid !== null}
        onClose={() => { setDeleteCid(null); setDeleteName(""); setDeleteUsername(""); setDeleteConfirm(""); }}
        title="危险操作：删除客户"
      >
        <div className="space-y-4">
          <div className="text-sm text-slate-300 p-4 glass-soft rounded-lg border border-loss/20">
            <div className="font-bold text-loss mb-2 flex items-center gap-1.5">
              <Trash2 size={15} /> 删除后不可恢复，请务必确认没有选错客户
            </div>
            <div>
              即将删除客户 <span className="text-loss font-bold">{deleteName}</span>，
              用户名 <span className="font-mono text-slate-100">{deleteUsername}</span>，
              ID: {deleteCid}
            </div>
            <div className="mt-2 text-xs text-slate-400 leading-relaxed">
              此操作会级联删除该客户的所有数据，包括：授权记录、策略配置、持仓、订单、交易记录、交易所账号、告警配置等。
            </div>
          </div>
          <Field label={`请输入用户名 ${deleteUsername} 以确认删除`}>
            <Input
              value={deleteConfirm}
              onChange={(e) => setDeleteConfirm(e.target.value)}
              placeholder={deleteUsername}
              autoFocus
            />
          </Field>
          <div className="text-xs text-slate-500">
            必须完整输入 <code className="text-loss font-mono">{deleteUsername}</code>，否则无法删除。
          </div>
          <div className="flex gap-2">
            <Button
              variant="ghost"
              className="flex-1"
              onClick={() => { setDeleteCid(null); setDeleteName(""); setDeleteUsername(""); setDeleteConfirm(""); }}
            >
              取消
            </Button>
            <Button
              variant="danger"
              className="flex-1"
              onClick={confirmDelete}
              disabled={deleteConfirm.trim() !== deleteUsername}
            >
              <Trash2 size={14} /> 确认删除
            </Button>
          </div>
        </div>
      </Modal>

      {/* 标签/备注编辑 Modal */}
      <Modal open={noteCid !== null} onClose={() => { setNoteCid(null); setNoteCustName(""); setNoteText(""); }} title={`编辑备注 - ${noteCustName}`}>
        <div className="space-y-4">
          <div className="text-xs text-slate-400 p-3 glass-soft rounded">
            为客户添加标签或备注,方便后期管理。例如:VIP客户、测试账号、到期续费等。
          </div>
          <Field label="备注内容">
            <textarea
              value={noteText}
              onChange={(e) => setNoteText(e.target.value)}
              placeholder="输入备注或标签..."
              rows={4}
              className="w-full bg-bg-soft border border-border/60 rounded-lg px-3 py-2 text-sm text-slate-200 focus:outline-none focus:border-accent/50 resize-none"
              autoFocus
            />
          </Field>
          <div className="flex gap-2">
            <Button variant="ghost" className="flex-1" onClick={() => { setNoteCid(null); setNoteCustName(""); setNoteText(""); }}>取消</Button>
            <Button className="flex-1" onClick={saveNote}>
              <StickyNote size={14} /> 保存备注
            </Button>
          </div>
        </div>
      </Modal>

      {/* 清除数据 Modal (支持全部/按客户) */}
      <Modal open={resetModal} onClose={() => { if (!resetting) { setResetModal(false); setResetConfirm(""); setResetCid(null); setResetCustName(""); } }} title={resetCid !== null ? `清除客户 ${resetCustName} 的数据` : "清除所有测试数据"} width="max-w-lg">
        <div className="space-y-4">
          <div className="text-sm text-slate-300 p-4 glass-soft rounded-lg border border-warn/30">
            <div className="font-bold text-warn mb-2 flex items-center gap-1.5">
              <Eraser size={15} /> 警告：此操作不可撤销
            </div>
            <div className="text-xs text-slate-400 leading-relaxed">
              {resetCid !== null ? (
                <>
                  将清除客户 <span className="text-warn font-semibold">{resetCustName}</span> 的以下数据：<br />
                  <span className="text-loss">订单、持仓、交易记录、挂单、告警日志、权益快照</span><br />
                  <span className="text-slate-500">（信号和审计日志为全局数据,不在按客户清除范围内）</span><br />
                  <br />
                  保留的配置数据：<br />
                  <span className="text-profit">客户账号、授权、KOL关注、交易所配置、风控配置、告警配置、策略配置</span>
                </>
              ) : (
                <>
                  将清除以下所有数据：<br />
                  <span className="text-loss">信号、订单、持仓、交易记录、挂单、告警日志、权益快照、审计日志</span><br />
                  <br />
                  保留的配置数据：<br />
                  <span className="text-profit">客户账号、授权、KOL、交易所配置、风控配置、告警配置、策略配置</span>
                </>
              )}
            </div>
          </div>
          <label className="flex items-center gap-2 text-sm text-slate-200">
            <input
              type="checkbox"
              className="accent-accent"
              checked={resetStrategy}
              onChange={(e) => setResetStrategy(e.target.checked)}
              disabled={resetting}
            />
            同时重置策略马丁格尔状态(配置保留)
          </label>
          <Field label="请输入确认文字">
            <Input
              value={resetConfirm}
              onChange={(e) => setResetConfirm(e.target.value)}
              placeholder={expectedResetText}
              disabled={resetting}
              autoFocus
            />
          </Field>
          <div className="text-xs text-slate-500">
            请输入 <code className="text-warn font-mono">{expectedResetText}</code> 以确认操作
          </div>
          <div className="flex gap-2">
            <Button variant="ghost" className="flex-1" onClick={() => { setResetModal(false); setResetConfirm(""); setResetCid(null); setResetCustName(""); }} disabled={resetting}>
              取消
            </Button>
            <Button
              variant="danger"
              className="flex-1"
              onClick={doResetData}
              disabled={resetting || resetConfirm !== expectedResetText}
            >
              {resetting ? "清除中..." : <><Eraser size={14} /> 确认清除</>}
            </Button>
          </div>
        </div>
      </Modal>
    </div>
  );
}
