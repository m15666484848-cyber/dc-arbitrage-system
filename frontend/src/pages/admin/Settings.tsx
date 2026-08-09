import { useEffect, useState } from "react";
import {
  Bot, Key, MessageSquare, Bell, Save, FlaskConical, Check, X, Plus, Trash2, Shield, Lock, Power,
} from "lucide-react";
import { API } from "@/api/client";
import { useFetch } from "@/lib/useFetch";
import { useToast } from "@/components/ui/Toast";
import { Card, CardTitle, Badge, Button, Empty, Input, Select, Field } from "@/components/ui";
import { Modal } from "@/components/ui/Modal";
export default function AdminSettings() {
  const [tab, setTab] = useState<"llm" | "discord" | "alert" | "security">("llm");
  return (
    <div className="space-y-6">
      <div>
        <h1 className="text-xl font-bold gradient-text flex items-center gap-2">系统设置</h1>
        <p className="text-sm text-slate-500 mt-1">LLM 智能解析 · Discord 监听 · 全局飞书告警 · 安全设置</p>
      </div>
      <div className="flex gap-2 overflow-x-auto">
        {[
          { k: "llm", label: "LLM 配置", icon: Bot },
          { k: "discord", label: "Discord 监听", icon: MessageSquare },
          { k: "alert", label: "全局告警", icon: Bell },
          { k: "security", label: "安全设置", icon: Shield },
        ].map((t) => (
          <button
            key={t.k}
            onClick={() => setTab(t.k as any)}
            className={`btn whitespace-nowrap transition-all duration-200 ${tab === t.k ? "bg-gold/15 text-gold border border-gold/50 shadow-[0_0_14px_-2px_rgba(245,158,11,0.25)]" : "bg-bg-hover text-slate-300 border border-transparent hover:border-border-soft"}`}
          >
            <t.icon size={15} /> {t.label}
          </button>
        ))}
      </div>
      {tab === "llm" && <LlmTab />}
      {tab === "discord" && <DiscordTab />}
      {tab === "alert" && <GlobalAlertTab />}
      {tab === "security" && <SecurityTab />}
    </div>
  );
}
// =============== LLM 配置(双模型) ===============
function LlmTab() {
  const { data, reload } = useFetch(() => API.getSystemConfig(), []);
  const { push } = useToast();
  const [f, setF] = useState<any>({
    llm_enabled: false,
    // 文本 LLM
    text_llm_provider: "deepseek",
    text_llm_api_key: null,
    text_llm_model: "",
    text_llm_api_base: "",
    text_llm_temperature: 0.1,
    text_llm_max_tokens: 2000,
    text_llm_timeout: 30,
    // 图片 LLM
    vision_llm_enabled: false,
    vision_llm_provider: "zhipu",
    vision_llm_api_key: null,
    vision_llm_model: "",
    vision_llm_api_base: "",
    vision_llm_temperature: 0.1,
    vision_llm_max_tokens: 2000,
    vision_llm_timeout: 60,
  });
  const [testing, setTesting] = useState<"text" | "vision" | null>(null);
  const [testResult, setTestResult] = useState<any>(null);
  useEffect(() => {
    if (data) {
      setF({
        llm_enabled: data.llm_enabled,
        text_llm_provider: data.text_llm_provider || "deepseek",
        text_llm_api_key: null,
        text_llm_model: data.text_llm_model || "",
        text_llm_api_base: data.text_llm_api_base || "",
        text_llm_temperature: data.text_llm_temperature ?? 0.1,
        text_llm_max_tokens: data.text_llm_max_tokens ?? 2000,
        text_llm_timeout: data.text_llm_timeout ?? 30,
        vision_llm_enabled: data.vision_llm_enabled || false,
        vision_llm_provider: data.vision_llm_provider || "zhipu",
        vision_llm_api_key: null,
        vision_llm_model: data.vision_llm_model || "",
        vision_llm_api_base: data.vision_llm_api_base || "",
        vision_llm_temperature: data.vision_llm_temperature ?? 0.1,
        vision_llm_max_tokens: data.vision_llm_max_tokens ?? 2000,
        vision_llm_timeout: data.vision_llm_timeout ?? 60,
      });
    }
  }, [data]);
  const save = async () => {
    try {
      await API.updateSystemConfig(f);
      push("success", "LLM 配置已保存,下次解析立即生效");
      setF({ ...f, text_llm_api_key: null, vision_llm_api_key: null });
      setTestResult(null);
      reload();
    } catch (e: any) {
      push("error", e?.response?.data?.message || "保存失败");
    }
  };
  const test = async (type: "text" | "vision") => {
    setTesting(type);
    setTestResult(null);
    try {
      await API.updateSystemConfig(f);
      const r: any = await API.testLlm(type);
      setTestResult({ ...r, type });
      push(r.success ? "success" : "error", r.message);
      reload();
    } catch (e: any) {
      setTestResult({ success: false, message: e?.response?.data?.message || "测试失败", type });
      push("error", "测试失败");
    } finally {
      setTesting(null);
    }
  };
  const cfg: any = data || {};
  return (
    <div className="space-y-4">
      {/* 全局开关 */}
      <Card className="relative overflow-hidden border-t border-t-gold/40">
        <CardTitle>LLM 智能解析 - 全局开关</CardTitle>
        <label className="flex items-center gap-3 p-3 glass-soft rounded-lg">
          <input
            type="checkbox"
            className="accent-accent w-4 h-4"
            checked={f.llm_enabled}
            onChange={(e) => setF({ ...f, llm_enabled: e.target.checked })}
          />
          <div className="flex-1">
            <div className="text-sm font-medium text-slate-100">启用 LLM 全局开关</div>
            <div className="text-xs text-slate-500">关闭后所有 LLM 调用都停止(节省 Token)</div>
          </div>
          <div className="flex gap-2">
            {cfg.text_llm_api_key_set ? (
              <Badge tone="profit"><Check size={11} /> 文本 Key 已配置</Badge>
            ) : (
              <Badge tone="loss"><X size={11} /> 文本 Key 未配置</Badge>
            )}
            {cfg.vision_llm_api_key_set ? (
              <Badge tone="profit"><Check size={11} /> 图片 Key 已配置</Badge>
            ) : (
              <Badge tone="loss"><X size={11} /> 图片 Key 未配置</Badge>
            )}
          </div>
        </label>
        <div className="mt-3 flex justify-end">
          <Button onClick={save}><Save size={15} /> 保存全部配置</Button>
        </div>
      </Card>
      {/* 文本 LLM 配置 */}
      <Card className="relative overflow-hidden border-t border-t-gold/40">
        <CardTitle action={
          <Button variant="ghost" onClick={() => test("text")} disabled={testing !== null}>
            <FlaskConical size={15} /> {testing === "text" ? "测试中..." : "测试文本 LLM"}
          </Button>
        }>
          文本 LLM (DeepSeek V3) - 解析信号文本
        </CardTitle>
        <div className="space-y-3">
          <div className="grid grid-cols-2 gap-3">
            <Field label="提供商">
              <Select value={f.text_llm_provider} onChange={(e) => setF({ ...f, text_llm_provider: e.target.value })}>
                <option value="deepseek">DeepSeek (推荐,便宜稳定)</option>
                <option value="zhipu">智谱 GLM-4 (文本)</option>
              </Select>
            </Field>
            <Field label={`API Key ${cfg.text_llm_api_key_mask ? `(当前: ${cfg.text_llm_api_key_mask})` : "(未配置)"}`}>
              <Input
                type="password"
                placeholder={cfg.text_llm_api_key_set ? "留空=不修改,输入新值=替换" : "粘贴 API Key"}
                value={f.text_llm_api_key || ""}
                onChange={(e) => setF({ ...f, text_llm_api_key: e.target.value || null })}
              />
            </Field>
          </div>
          <div className="grid grid-cols-2 gap-3">
            <Field label="模型名称(留空用默认)">
              <Input
                value={f.text_llm_model}
                onChange={(e) => setF({ ...f, text_llm_model: e.target.value })}
                placeholder={f.text_llm_provider === "deepseek" ? "deepseek-chat (V3)" : "glm-4"}
              />
            </Field>
            <Field label="API Base(留空用默认)">
              <Input
                value={f.text_llm_api_base}
                onChange={(e) => setF({ ...f, text_llm_api_base: e.target.value })}
                placeholder={f.text_llm_provider === "deepseek" ? "https://api.deepseek.com/v1" : "https://open.bigmodel.cn/api/paas/v4"}
              />
            </Field>
          </div>
          <div className="grid grid-cols-3 gap-3">
            <Field label={`温度 (${f.text_llm_temperature})`}>
              <input type="range" min="0" max="1" step="0.1" value={f.text_llm_temperature}
                onChange={(e) => setF({ ...f, text_llm_temperature: parseFloat(e.target.value) })}
                className="w-full accent-accent" />
            </Field>
            <Field label="最大 Tokens">
              <Input type="number" value={f.text_llm_max_tokens}
                onChange={(e) => setF({ ...f, text_llm_max_tokens: Number(e.target.value) })} />
            </Field>
            <Field label="超时(秒)">
              <Input type="number" value={f.text_llm_timeout}
                onChange={(e) => setF({ ...f, text_llm_timeout: Number(e.target.value) })} />
            </Field>
          </div>
          {testResult && testResult.type === "text" && (
            <div className={`p-3 rounded-lg text-sm ${testResult.success ? "bg-profit/10 border border-profit/20 text-profit" : "bg-loss/10 border border-loss/20 text-loss"}`}>
              <div className="font-medium">{testResult.success ? "文本 LLM 测试成功" : "文本 LLM 测试失败"}</div>
              <div className="text-xs mt-1">{testResult.message}</div>
              {testResult.latency_ms > 0 && (
                <div className="text-xs mt-1 text-slate-500">延迟: {testResult.latency_ms}ms / Tokens: {testResult.tokens_used}</div>
              )}
            </div>
          )}
        </div>
      </Card>
      {/* 图片 LLM 配置 */}
      <Card className="relative overflow-hidden border-t border-t-gold/40">
        <CardTitle action={
          <Button variant="ghost" onClick={() => test("vision")} disabled={testing !== null}>
            <FlaskConical size={15} /> {testing === "vision" ? "测试中..." : "测试图片 LLM"}
          </Button>
        }>
          图片 LLM (GLM-4V) - 解析图片信号
        </CardTitle>
        <div className="space-y-3">
          <label className="flex items-center gap-3 p-3 glass-soft rounded-lg">
            <input
              type="checkbox"
              className="accent-accent w-4 h-4"
              checked={f.vision_llm_enabled}
              onChange={(e) => setF({ ...f, vision_llm_enabled: e.target.checked })}
            />
            <div className="flex-1">
              <div className="text-sm font-medium text-slate-100">启用图片 LLM 全局开关</div>
              <div className="text-xs text-slate-500">
                开启后,需在 KOL 管理页面对指定 KOL 勾选"启用图片 LLM"才会调用(节省 Token)
              </div>
            </div>
          </label>
          <div className="grid grid-cols-2 gap-3">
            <Field label="提供商">
              <Select value={f.vision_llm_provider} onChange={(e) => setF({ ...f, vision_llm_provider: e.target.value })}>
                <option value="zhipu">智谱 GLM-4V (直连,多模态)</option>
                <option value="siliconflow">SiliconFlow 中转 (GLM-4.5V)</option>
                <option value="deepseek">DeepSeek (暂不支持图片)</option>
              </Select>
            </Field>
            <Field label={`API Key ${cfg.vision_llm_api_key_mask ? `(当前: ${cfg.vision_llm_api_key_mask})` : "(未配置,留空则复用文本 LLM Key)"}`}>
              <Input
                type="password"
                placeholder={cfg.vision_llm_api_key_set ? "留空=不修改,输入新值=替换" : "粘贴 API Key(留空复用文本 Key)"}
                value={f.vision_llm_api_key || ""}
                onChange={(e) => setF({ ...f, vision_llm_api_key: e.target.value || null })}
              />
            </Field>
          </div>
          <div className="grid grid-cols-2 gap-3">
            <Field label="模型名称(留空用默认)">
              <Input
                value={f.vision_llm_model}
                onChange={(e) => setF({ ...f, vision_llm_model: e.target.value })}
                placeholder={f.vision_llm_provider === "zhipu" ? "glm-4v" : f.vision_llm_provider === "siliconflow" ? "zai-org/GLM-4.5V" : "deepseek-chat"}
              />
            </Field>
            <Field label="API Base(留空用默认)">
              <Input
                value={f.vision_llm_api_base}
                onChange={(e) => setF({ ...f, vision_llm_api_base: e.target.value })}
                placeholder={f.vision_llm_provider === "zhipu" ? "https://open.bigmodel.cn/api/paas/v4" : f.vision_llm_provider === "siliconflow" ? "https://api.siliconflow.cn/v1" : "https://api.deepseek.com/v1"}
              />
            </Field>
          </div>
          <div className="grid grid-cols-3 gap-3">
            <Field label={`温度 (${f.vision_llm_temperature})`}>
              <input type="range" min="0" max="1" step="0.1" value={f.vision_llm_temperature}
                onChange={(e) => setF({ ...f, vision_llm_temperature: parseFloat(e.target.value) })}
                className="w-full accent-accent" />
            </Field>
            <Field label="最大 Tokens">
              <Input type="number" value={f.vision_llm_max_tokens}
                onChange={(e) => setF({ ...f, vision_llm_max_tokens: Number(e.target.value) })} />
            </Field>
            <Field label="超时(秒)">
              <Input type="number" value={f.vision_llm_timeout}
                onChange={(e) => setF({ ...f, vision_llm_timeout: Number(e.target.value) })} />
            </Field>
          </div>
          {testResult && testResult.type === "vision" && (
            <div className={`p-3 rounded-lg text-sm ${testResult.success ? "bg-profit/10 border border-profit/20 text-profit" : "bg-loss/10 border border-loss/20 text-loss"}`}>
              <div className="font-medium">{testResult.success ? "图片 LLM 测试成功" : "图片 LLM 测试失败"}</div>
              <div className="text-xs mt-1">{testResult.message}</div>
              {testResult.latency_ms > 0 && (
                <div className="text-xs mt-1 text-slate-500">延迟: {testResult.latency_ms}ms / Tokens: {testResult.tokens_used}</div>
              )}
            </div>
          )}
        </div>
      </Card>
      <div className="text-xs text-slate-500 bg-warn/10 border border-warn/20 rounded-lg p-3">
        <strong>双 LLM 架构说明:</strong><br />
        • <strong>文本 LLM (DeepSeek V3)</strong>:解析 KOL 发的文本信号,规则解析失败时兜底<br />
        • <strong>图片 LLM (GLM-4V)</strong>:直接分析图片内容,不经过 OCR,需在 KOL 管理页对指定 KOL 勾选<br />
        • 两个模型独立计费,图片 LLM 仅对勾选的 KOL 生效(节省 Token)<br />
        • 图片 LLM Key 留空会自动复用文本 LLM 的 Key(若同提供商)<br />
        • 修改后立即生效,无需重启服务
      </div>
    </div>
  );
}
// =============== Discord 配置 ===============
const DISCORD_ACCOUNT_EMPTY = {
  label: "",
  token: "",
  enabled: true,
  is_default: false,
};

function DiscordAccountManager() {
  const { data, reload } = useFetch(() => API.listDiscordAccounts(), []);
  const { push } = useToast();
  const [modal, setModal] = useState(false);
  const [editId, setEditId] = useState<number | null>(null);
  const [f, setF] = useState<any>({ ...DISCORD_ACCOUNT_EMPTY });
  const list: any[] = data || [];

  const open = (account?: any) => {
    if (account) {
      setEditId(account.id);
      setF({
        label: account.label || "",
        token: "",
        enabled: account.enabled,
        is_default: account.is_default,
      });
    } else {
      setEditId(null);
      setF({ ...DISCORD_ACCOUNT_EMPTY, is_default: list.length === 0 });
    }
    setModal(true);
  };

  const save = async () => {
    try {
      if (!f.label?.trim()) {
        push("error", "请填写账号名称");
        return;
      }
      if (!editId && !f.token?.trim()) {
        push("error", "新增账号必须填写 Discord Token");
        return;
      }

      const payload: any = {
        label: f.label.trim(),
        enabled: !!f.enabled,
        is_default: !!f.is_default,
      };
      if (f.token?.trim()) payload.token = f.token.trim();

      if (editId) await API.updateDiscordAccount(editId, payload);
      else await API.createDiscordAccount(payload);

      push("success", "Discord 账号已保存，监听服务将在 60 秒内自动加载");
      setModal(false);
      reload();
    } catch (e: any) {
      push("error", e?.response?.data?.message || "保存失败");
    }
  };

  const toggle = async (account: any) => {
    try {
      await API.updateDiscordAccount(account.id, { enabled: !account.enabled });
      push("success", account.enabled ? "已停用" : "已启用");
      reload();
    } catch (e: any) {
      push("error", e?.response?.data?.message || "操作失败");
    }
  };

  const setDefault = async (account: any) => {
    try {
      await API.updateDiscordAccount(account.id, { is_default: true });
      push("success", "默认 Discord 账号已更新");
      reload();
    } catch (e: any) {
      push("error", e?.response?.data?.message || "设置失败");
    }
  };

  const remove = async (account: any) => {
    if (!window.confirm(`确定删除 Discord 账号「${account.label}」? 已绑定该账号的 KOL 将不能继续用它监听。`)) return;
    try {
      await API.deleteDiscordAccount(account.id);
      push("success", "Discord 账号已删除");
      reload();
    } catch (e: any) {
      push("error", e?.response?.data?.message || "删除失败");
    }
  };

  return (
    <Card className="relative overflow-hidden border-t border-t-accent-glow/40">
      <CardTitle action={<Button onClick={() => open()}><Plus size={15} /> 添加账号</Button>}>
        Discord 多账号 Token
      </CardTitle>

      <div className="space-y-3">
        {list.length === 0 ? (
          <Empty text="尚未配置 Discord 账号" />
        ) : (
          list.map((account) => (
            <div key={account.id} className="glass-soft p-3 rounded-xl">
              <div className="flex items-start justify-between gap-3">
                <div className="min-w-0">
                  <div className="flex flex-wrap items-center gap-2">
                    <span className="font-semibold text-slate-100">{account.label}</span>
                    {account.is_default && <Badge tone="accent">默认</Badge>}
                    {account.enabled ? <Badge tone="profit"><Check size={11} /> 启用</Badge> : <Badge tone="loss"><X size={11} /> 停用</Badge>}
                    {account.token_set ? <Badge tone="profit">Token 已配置</Badge> : <Badge tone="loss">Token 未配置</Badge>}
                  </div>
                  <div className="mt-2 text-xs text-slate-500 space-y-1">
                    <div>Token：<span className="font-mono text-slate-300">{account.token_mask || "—"}</span></div>
                    <div>最近连接：<span className="text-slate-300">{account.last_connected_at ? new Date(account.last_connected_at).toLocaleString() : "暂无"}</span></div>
                    {account.last_error && <div className="text-loss">错误：{account.last_error}</div>}
                  </div>
                </div>
                <div className="flex flex-wrap gap-1 justify-end">
                  {!account.is_default && account.enabled && (
                    <button className="px-2 py-1 rounded-lg text-xs bg-accent/10 text-accent hover:bg-accent/20" onClick={() => setDefault(account)}>
                      设为默认
                    </button>
                  )}
                  <button className="px-2 py-1 rounded-lg text-xs bg-slate-700/40 text-slate-300 hover:bg-slate-700" onClick={() => open(account)}>
                    编辑
                  </button>
                  <button className="px-2 py-1 rounded-lg text-xs bg-warn/10 text-warn hover:bg-warn/20" onClick={() => toggle(account)}>
                    {account.enabled ? "停用" : "启用"}
                  </button>
                  <button className="px-2 py-1 rounded-lg text-xs bg-loss/10 text-loss hover:bg-loss/20" onClick={() => remove(account)}>
                    删除
                  </button>
                </div>
              </div>
            </div>
          ))
        )}
      </div>

      <Modal open={modal} onClose={() => setModal(false)} title={editId ? "编辑 Discord 账号" : "添加 Discord 账号"}>
        <div className="space-y-4">
          <Field label="账号名称">
            <Input value={f.label} onChange={(e) => setF({ ...f, label: e.target.value })} placeholder="例如：A 账号 / B 账号 / 三姐群账号" />
          </Field>
          <Field label={editId ? "Discord Token（留空则不修改）" : "Discord Token"}>
            <Input
              type="password"
              value={f.token}
              onChange={(e) => setF({ ...f, token: e.target.value })}
              placeholder={editId ? "留空=不修改,输入新值=替换" : "粘贴 Discord 用户 Token"}
            />
          </Field>
          <label className="flex items-center gap-2 text-sm text-slate-300">
            <input type="checkbox" className="accent-accent" checked={f.enabled} onChange={(e) => setF({ ...f, enabled: e.target.checked })} /> 启用监听
          </label>
          <label className="flex items-center gap-2 text-sm text-slate-300">
            <input type="checkbox" className="accent-accent" checked={f.is_default} onChange={(e) => setF({ ...f, is_default: e.target.checked })} /> 设为默认账号
          </label>
          <div className="text-xs text-slate-500 bg-warn/10 border border-warn/20 rounded-lg p-3">
            KOL 绑定到哪个账号，就使用哪个账号的 Token 监听该 KOL 频道。修改 Token 后监听服务会在 60 秒内自动加载。
          </div>
          <Button className="w-full" onClick={save}>保存</Button>
        </div>
      </Modal>
    </Card>
  );
}

function DiscordTab() {
  const { data, reload } = useFetch(() => API.getSystemConfig(), []);
  const { push } = useToast();
  const [token, setToken] = useState<string | null>(null);
  const [heartbeat, setHeartbeat] = useState(41);

  useEffect(() => {
    if (data) {
      setToken(null);
      setHeartbeat(data.discord_heartbeat_interval || 41);
    }
  }, [data]);

  const save = async () => {
    try {
      await API.updateSystemConfig({
        discord_token: token,
        discord_heartbeat_interval: heartbeat,
      });
      push("success", "Discord 兼容配置已保存,监听服务将在 60 秒内自动重连");
      setToken(null);
      reload();
    } catch (e: any) {
      push("error", e?.response?.data?.message || "保存失败");
    }
  };

  const cfg: any = data || {};

  return (
    <div className="space-y-4">
      <DiscordAccountManager />

      <Card className="relative overflow-hidden border-t border-t-accent-glow/40">
        <CardTitle action={<Button onClick={save}><Save size={15} /> 保存</Button>}>
          Discord 兼容配置（旧版单 Token）
        </CardTitle>
        <div className="space-y-4">
          <Field label={`用户 Token ${cfg.discord_token_mask ? `(当前: ${cfg.discord_token_mask})` : "(未配置)"}`}>
            <Input
              type="password"
              placeholder={cfg.discord_token_set ? "留空=不修改,输入新值=替换" : "粘贴 Discord 用户 Token"}
              value={token || ""}
              onChange={(e) => setToken(e.target.value || null)}
            />
          </Field>

          <div className="flex items-center gap-2">
            {cfg.discord_token_set ? (
              <Badge tone="profit"><Check size={11} /> 旧版 Token 已配置</Badge>
            ) : (
              <Badge tone="loss"><X size={11} /> 旧版 Token 未配置</Badge>
            )}
          </div>

          <Field label="心跳间隔(毫秒,Discord 推荐 41250)">
            <Input
              type="number"
              value={heartbeat}
              onChange={(e) => setHeartbeat(Number(e.target.value))}
            />
          </Field>

          <div className="text-xs text-slate-500 bg-warn/10 border border-warn/20 rounded-lg p-3">
            <strong>Discord 用户 Token 获取方式:</strong><br />
            1. 浏览器登录 Discord 网页版<br />
            2. F12 打开开发者工具 → Network 标签<br />
            3. 刷新页面,找任意请求 → Headers → Authorization<br />
            4. 复制值粘贴到上方输入框<br /><br />
            <strong>说明:</strong> 上方“Discord 多账号 Token”用于新的 KOL 绑定监听；这里保留旧版单 Token 配置用于兼容未绑定账号的历史逻辑。
          </div>
        </div>
      </Card>
    </div>
  );
}

// =============== 全局告警 ===============
function GlobalAlertTab() {
  const { data, reload } = useFetch(() => API.listAlerts(), []);
  const { push } = useToast();
  const [modal, setModal] = useState(false);
  const [f, setF] = useState({
    name: "全局飞书告警", webhook_url: "",
    on_signal: true, on_order: true, on_tp_sl: true, on_correct: true,
    on_risk: true, on_error: true, on_auth_expire: true,
  });
  // 仅显示 customer_id 为 null 的全局告警
  const list: any[] = (data || []).filter((x: any) => x.customer_id === null);
  const add = async () => {
    try {
      await API.addAlert(f);
      push("success", "全局告警已添加");
      setModal(false);
      setF({
        name: "全局飞书告警", webhook_url: "",
        on_signal: true, on_order: true, on_tp_sl: true, on_correct: true,
        on_risk: true, on_error: true, on_auth_expire: true,
      });
      reload();
    } catch (e: any) {
      push("error", e?.response?.data?.message || "失败");
    }
  };
  const toggle = async (id: number, enabled: boolean) => {
    try {
      await API.toggleAlert(id);
      push("success", enabled ? "已停用" : "已启用");
      reload();
    } catch (e: any) {
      push("error", e?.response?.data?.message || "操作失败");
    }
  };
  const remove = async (id: number, name: string) => {
    if (!window.confirm(`确定删除告警「${name}」?此操作不可撤销。`)) return;
    try {
      await API.deleteAlert(id);
      push("success", "已删除");
      reload();
    } catch (e: any) {
      push("error", e?.response?.data?.message || "删除失败");
    }
  };
  return (
    <Card className="relative overflow-hidden border-t border-t-gold/40">
      <CardTitle action={<Button onClick={() => setModal(true)}><Plus size={15} /> 添加全局告警</Button>}>
        全局飞书告警(管理员级,所有客户事件都推送)
      </CardTitle>
      {list.length === 0 ? (
        <Empty text="尚未配置全局告警" />
      ) : (
        <div className="space-y-2">
          {list.map((a) => (
            <div key={a.id} className="glass-soft p-3 flex items-center gap-3">
              <Bell size={16} className={a.enabled ? "text-accent-glow" : "text-slate-600"} />
              <div className="flex-1 min-w-0">
                <div className={`text-sm font-medium ${a.enabled ? "text-slate-100" : "text-slate-500"}`}>{a.name}</div>
                <div className="text-xs text-slate-500 font-mono truncate">{a.webhook_url}</div>
              </div>
              <button
                onClick={() => toggle(a.id, a.enabled)}
                title={a.enabled ? "点击停用" : "点击启用"}
                className={`px-2.5 py-1 rounded-lg text-xs font-semibold flex items-center gap-1 transition ${
                  a.enabled
                    ? "bg-profit/15 text-profit hover:bg-profit/25"
                    : "bg-slate-600/30 text-slate-400 hover:bg-slate-600/50"
                }`}
              >
                <Power size={12} />
                {a.enabled ? "启用中" : "已停用"}
              </button>
              <button
                onClick={() => remove(a.id, a.name)}
                title="删除"
                className="text-slate-400 hover:text-loss p-1.5 rounded-lg hover:bg-loss/10 transition"
              >
                <Trash2 size={15} />
              </button>
            </div>
          ))}
        </div>
      )}
      <Modal open={modal} onClose={() => setModal(false)} title="添加全局飞书告警">
        <div className="space-y-4">
          <Field label="告警名称">
            <Input value={f.name} onChange={(e) => setF({ ...f, name: e.target.value })} />
          </Field>
          <Field label="飞书 Webhook URL">
            <Input
              value={f.webhook_url}
              onChange={(e) => setF({ ...f, webhook_url: e.target.value })}
              placeholder="https://open.feishu.cn/open-apis/bot/v2/hook/xxx"
            />
          </Field>
          <div className="grid grid-cols-2 gap-2">
            {[
              ["on_signal", "收到信号"], ["on_order", "下单成交"], ["on_tp_sl", "止盈止损"],
              ["on_correct", "信号纠错"], ["on_risk", "风控熔断"], ["on_error", "系统错误"],
              ["on_auth_expire", "授权到期"],
            ].map(([k, l]) => (
              <label key={k} className="flex items-center gap-2 text-sm text-slate-300">
                <input
                  type="checkbox"
                  className="accent-accent"
                  checked={(f as any)[k]}
                  onChange={(e) => setF({ ...f, [k]: e.target.checked })}
                />
                {l}
              </label>
            ))}
          </div>
          <Button className="w-full" onClick={add}>保存</Button>
        </div>
      </Modal>
    </Card>
  );
}
// =============== 安全设置(修改密码) ===============
function SecurityTab() {
  const { push } = useToast();
  const [oldPwd, setOldPwd] = useState("");
  const [newPwd, setNewPwd] = useState("");
  const [confirmPwd, setConfirmPwd] = useState("");
  const [loading, setLoading] = useState(false);
  const submit = async () => {
    if (!oldPwd || !newPwd || !confirmPwd) {
      push("error", "请填写所有字段");
      return;
    }
    if (newPwd.length < 6) {
      push("error", "新密码至少 6 位");
      return;
    }
    if (newPwd !== confirmPwd) {
      push("error", "两次输入的新密码不一致");
      return;
    }
    if (oldPwd === newPwd) {
      push("error", "新密码不能与旧密码相同");
      return;
    }
    setLoading(true);
    try {
      await API.changePassword(oldPwd, newPwd);
      push("success", "密码修改成功,请妥善保管");
      setOldPwd("");
      setNewPwd("");
      setConfirmPwd("");
    } catch (e: any) {
      const msg = e?.response?.data?.detail || e?.response?.data?.message || "密码修改失败";
      push("error", msg);
    } finally {
      setLoading(false);
    }
  };
  return (
    <Card>
      <CardTitle>
        <div className="flex items-center gap-2">
          <Lock size={18} className="text-gold" />
          <span>修改管理员密码</span>
        </div>
      </CardTitle>
      <div className="space-y-4 max-w-md">
        <div className="text-xs text-slate-400 p-3 glass-soft rounded-lg">
          修改密码后需要重新登录。建议定期更换密码以确保账号安全。
        </div>
        <Field label="当前密码">
          <Input
            type="password"
            value={oldPwd}
            onChange={(e) => setOldPwd(e.target.value)}
            placeholder="输入当前密码"
          />
        </Field>
        <Field label="新密码">
          <Input
            type="password"
            value={newPwd}
            onChange={(e) => setNewPwd(e.target.value)}
            placeholder="输入新密码 (至少 6 位)"
          />
        </Field>
        <Field label="确认新密码">
          <Input
            type="password"
            value={confirmPwd}
            onChange={(e) => setConfirmPwd(e.target.value)}
            placeholder="再次输入新密码"
          />
        </Field>
        <Button className="w-full" onClick={submit} disabled={loading}>
          {loading ? "提交中..." : "确认修改"}
        </Button>
      </div>
    </Card>
  );
}
