import { useState } from "react";
import { Crown, Plus, Pencil, Power, Bot, Settings, Trash2 } from "lucide-react";
import { API } from "@/api/client";
import { useFetch } from "@/lib/useFetch";
import { useToast } from "@/components/ui/Toast";
import { Card, CardTitle, Badge, Button, Empty, Input, Select, Field } from "@/components/ui";
import { Modal } from "@/components/ui/Modal";
import { fmtMoney } from "@/lib/utils";
const EMPTY = {
  name: "",
  discord_account_id: null as number | null,
  discord_channel_id: "",
  discord_user_id: "",
  enabled: true,
  avatar: "",
  description: "",
  llm_enabled: false,
  vision_llm_enabled: false,
  llm_fallback: true,
  llm_min_confidence: 0.4,
};
export default function AdminKols() {
  const { data, reload } = useFetch(() => API.listAdminKols(), []);
  const { data: discordAccountsData } = useFetch(() => API.listDiscordAccounts(), []);
  const { push } = useToast();
  const [modal, setModal] = useState(false);
  const [editId, setEditId] = useState<number | null>(null);
  const [f, setF] = useState<any>({ ...EMPTY });
  const list: any[] = (data || []).filter((k: any) => k.enabled);
  const totalSignals = list.reduce((sum: number, k: any) => sum + (k.cached_signal_count || 0), 0);
  const avgWinRate = list.length ? (list.reduce((sum: number, k: any) => sum + (k.cached_win_rate || 0), 0) / list.length).toFixed(1) : "0.0";
  const totalPnl = list.reduce((sum: number, k: any) => sum + (k.cached_pnl || 0), 0);
  const discordAccounts: any[] = discordAccountsData || [];
  const enabledDiscordAccounts = discordAccounts.filter((account: any) => account.enabled);
  const defaultDiscordAccount = enabledDiscordAccounts.find((account: any) => account.is_default) || enabledDiscordAccounts[0];

  const getDiscordAccountLabel = (accountId?: number | null) => {
    const account = discordAccounts.find((item: any) => item.id === accountId);
    if (account) return `${account.label}${account.is_default ? "（默认）" : ""}`;
    if (!accountId && defaultDiscordAccount) return `${defaultDiscordAccount.label}（默认）`;
    return accountId ? `账号 #${accountId}` : "默认账号";
  };
  const open = (k?: any) => {
    if (k) {
      setEditId(k.id);
      setF({ ...k, discord_account_id: k.discord_account_id ?? null });
    } else {
      setEditId(null);
      setF({ ...EMPTY, discord_account_id: defaultDiscordAccount?.id ?? null });
    }
    setModal(true);
  };
  const save = async () => {
    try {
      const payload = {
        ...f,
        discord_account_id: f.discord_account_id ? Number(f.discord_account_id) : null,
      };
      if (editId) await API.updateKol(editId, payload);
      else await API.createKol(payload);
      push("success", "已保存");
      setModal(false);
      reload();
    } catch (e: any) {
      push("error", e?.response?.data?.message || "保存失败");
    }
  };
  const toggle = async (k: any) => {
    try { await API.updateKol(k.id, { enabled: !k.enabled }); reload(); } catch {}
  };
  const remove = async (k: any) => {
    if (!window.confirm(`确定删除 KOL "${k.name}"? (软删除,数据可恢复)`)) return;
    try {
      await API.deleteKol(k.id);
      push("success", "已删除");
      reload();
    } catch (e: any) {
      push("error", e?.response?.data?.message || "删除失败");
    }
  };
  return (
    <div className="space-y-6">
      <div className="flex items-center justify-between">
        <div>
          <h1 className="text-xl font-bold gradient-text flex items-center gap-2">KOL 管理</h1>
          <p className="text-sm text-slate-500 mt-1">配置 Discord 频道号与 KOL 档案(客户不可见)</p>
        </div>
        <Button onClick={() => open()}><Plus size={15} /> 添加 KOL</Button>
      </div>
      {/* KPI 概览 */}
      <div className="grid grid-cols-2 md:grid-cols-4 gap-3">
        <div className="glass p-4"><div className="text-xs text-slate-500 mb-1">KOL 总数</div><div className="text-xl md:text-2xl font-bold font-mono text-slate-100">{list.length}</div></div>
        <div className="glass p-4"><div className="text-xs text-slate-500 mb-1">信号总数</div><div className="text-xl md:text-2xl font-bold font-mono text-accent-glow">{totalSignals}</div></div>
        <div className="glass p-4"><div className="text-xs text-slate-500 mb-1">平均胜率</div><div className="text-xl md:text-2xl font-bold font-mono text-profit">{avgWinRate}%</div></div>
        <div className="glass p-4"><div className="text-xs text-slate-500 mb-1">总盈亏</div><div className={`text-xl md:text-2xl font-bold font-mono ${totalPnl >= 0 ? "text-profit" : "text-loss"}`}>{fmtMoney(totalPnl, 0)}</div></div>
      </div>
      <div className="grid md:grid-cols-2 xl:grid-cols-3 gap-4">
        {list.length === 0 ? (
          <div className="col-span-full"><Card><Empty text="暂无 KOL,先添加一个" /></Card></div>
        ) : (
          list.map((k) => (
            <Card key={k.id}>
              <div className="flex items-start justify-between">
                <div className="flex items-center gap-3">
                  <div className="w-11 h-11 rounded-xl bg-gradient-to-br from-accent/30 to-accent-glow/20 flex items-center justify-center text-accent-glow font-bold ring-1 ring-gold/40 shadow-glow">
                    {k.avatar ? <img src={k.avatar} className="w-full h-full rounded-xl object-cover" /> : k.name?.[0] || "K"}
                  </div>
                  <div>
                    <div className="font-semibold text-slate-100">{k.name}</div>
                    <Badge tone={k.enabled ? "profit" : "default"}>{k.enabled ? "启用" : "停用"}</Badge>
                  </div>
                </div>
                <div className="flex gap-1">
                  <button onClick={() => open(k)} className="text-slate-400 hover:text-accent-glow p-1" title="编辑"><Pencil size={14} /></button>
                  <button onClick={() => toggle(k)} className="text-slate-400 hover:text-warn p-1" title={k.enabled ? "停用" : "启用"}><Power size={14} /></button>
                  <button onClick={() => remove(k)} className="text-slate-400 hover:text-danger p-1" title="删除"><Trash2 size={14} /></button>
                </div>
              </div>
              <div className="mt-3 space-y-1.5 text-xs">
                <div className="flex justify-between"><span className="text-slate-500">监听账号</span><span className="text-slate-300">{getDiscordAccountLabel(k.discord_account_id)}</span></div>
                <div className="flex justify-between"><span className="text-slate-500">频道 ID</span><span className="font-mono text-slate-300">{k.discord_channel_id || "—"}</span></div>
                <div className="flex justify-between"><span className="text-slate-500">用户 ID</span><span className="font-mono text-slate-300">{k.discord_user_id || "全部"}</span></div>
                <div className="flex justify-between"><span className="text-slate-500">信号数</span><span className="text-slate-300">{k.cached_signal_count}</span></div>
                <div className="flex justify-between"><span className="text-slate-500">胜率</span><span className="text-slate-300">{k.cached_win_rate}%</span></div>
                <div className="flex justify-between"><span className="text-slate-500">总盈亏</span><span className="text-slate-300">{fmtMoney(k.cached_pnl, 0)}</span></div>
              </div>
              {/* LLM 配置状态 */}
              {k.llm_enabled && (
                <div className="mt-3 p-2 glass-soft rounded-lg">
                  <div className="flex items-center gap-2 text-xs text-accent-glow mb-1">
                    <Bot size={12} />
                    <span className="font-semibold">LLM 解析已启用</span>
                  </div>
                  <div className="flex flex-wrap gap-1">
                    {k.vision_llm_enabled && <Badge tone="accent" className="text-[10px] px-1.5 py-0.5">图片 LLM</Badge>}
                    {k.llm_fallback && <Badge tone="accent" className="text-[10px] px-1.5 py-0.5">文本兜底</Badge>}
                    <span className="text-[10px] text-slate-500">阈值: {k.llm_min_confidence}</span>
                    {k.llm_calls_total > 0 && (
                      <span className="text-[10px] text-slate-500">
                        调用: {k.llm_calls_total}次 ({k.llm_tokens_used || 0} tokens)
                      </span>
                    )}
                  </div>
                </div>
              )}
              {k.description && <p className="text-xs text-slate-500 mt-2">{k.description}</p>}
            </Card>
          ))
        )}
      </div>
      <Modal open={modal} onClose={() => setModal(false)} title={editId ? "编辑 KOL" : "添加 KOL"}>
        <div className="space-y-4 max-h-[70vh] overflow-y-auto">
          <Field label="KOL 名称"><Input value={f.name} onChange={(e) => setF({ ...f, name: e.target.value })} /></Field>
          <Field label="监听 Discord 账号">
            <Select
              value={f.discord_account_id ?? ""}
              onChange={(e) => setF({ ...f, discord_account_id: e.target.value ? Number(e.target.value) : null })}
            >
              <option value="">默认账号</option>
              {enabledDiscordAccounts.map((account: any) => (
                <option key={account.id} value={account.id}>
                  {account.label}{account.is_default ? "（默认）" : ""}{account.token_mask ? ` · ${account.token_mask}` : ""}
                </option>
              ))}
            </Select>
            {enabledDiscordAccounts.length === 0 && <p className="mt-1 text-xs text-warn">还没有启用的 Discord 账号，请先到系统设置里添加 Token。</p>}
          </Field>
          <Field label="Discord 频道 ID"><Input value={f.discord_channel_id} onChange={(e) => setF({ ...f, discord_channel_id: e.target.value })} placeholder="123456789012345678" /></Field>
          <Field label="Discord 用户 ID(留空则监听该频道所有人)"><Input value={f.discord_user_id} onChange={(e) => setF({ ...f, discord_user_id: e.target.value })} /></Field>
          <Field label="头像 URL(可选)"><Input value={f.avatar} onChange={(e) => setF({ ...f, avatar: e.target.value })} /></Field>
          <Field label="描述"><Input value={f.description} onChange={(e) => setF({ ...f, description: e.target.value })} /></Field>
          <label className="flex items-center gap-2 text-sm text-slate-300">
            <input type="checkbox" className="accent-accent" checked={f.enabled} onChange={(e) => setF({ ...f, enabled: e.target.checked })} /> 启用监听
          </label>
          {/* LLM 配置区域 */}
          <div className="pt-4 border-t border-border-soft">
            <div className="flex items-center gap-2 mb-3 text-sm font-semibold text-slate-200">
              <Bot size={16} className="text-accent-glow" />
              <span>LLM 智能解析配置</span>
            </div>
            <p className="text-xs text-slate-500 mb-3">
              仅对特定 KOL 启用 LLM 可节省 Token，只在需要时开启
            </p>
            
            <label className="flex items-center gap-2 text-sm text-slate-300 mb-2">
              <input type="checkbox" className="accent-accent" checked={f.llm_enabled} onChange={(e) => setF({ ...f, llm_enabled: e.target.checked })} /> 
              <span className="font-medium">启用 LLM 解析</span>
            </label>
            {f.llm_enabled && (
              <div className="ml-4 space-y-3 p-3 glass-soft rounded-lg">
                <label className="flex items-center gap-2 text-sm text-slate-300">
                  <input type="checkbox" className="accent-accent" checked={f.vision_llm_enabled} onChange={(e) => setF({ ...f, vision_llm_enabled: e.target.checked })} />
                  <span>启用图片 LLM 分析（GLM-4V,走全局图片模型配置）</span>
                </label>
                <label className="flex items-center gap-2 text-sm text-slate-300">
                  <input type="checkbox" className="accent-accent" checked={f.llm_fallback} onChange={(e) => setF({ ...f, llm_fallback: e.target.checked })} />
                  <span>规则解析失败时降级到文本 LLM (DeepSeek V3)</span>
                </label>
                <Field label={`置信度阈值 (${f.llm_min_confidence})`}>
                  <input
                    type="range"
                    min="0.1"
                    max="0.8"
                    step="0.1"
                    value={f.llm_min_confidence}
                    onChange={(e) => setF({ ...f, llm_min_confidence: parseFloat(e.target.value) })}
                    className="w-full accent-accent"
                  />
                  <div className="flex justify-between text-xs text-slate-500 mt-1">
                    <span>严格 (0.1)</span>
                    <span>宽松 (0.8)</span>
                  </div>
                </Field>
                <div className="text-xs text-slate-500 mt-2 p-2 bg-bg-dark rounded">
                  <strong>双 LLM 架构说明：</strong><br/>
                  • <strong>文本 LLM (DeepSeek V3)</strong>：规则解析失败时兜底,解析 KOL 文本信号<br/>
                  • <strong>图片 LLM (GLM-4V)</strong>：直接分析图片内容,需在此勾选才对该 KOL 生效<br/>
                  • 两个模型在「系统设置」统一配置,这里只控制该 KOL 是否启用<br/>
                  • 只发图片的 KOL：勾选"图片 LLM",可关闭"文本兜底"<br/>
                  • 文本复杂的 KOL：不勾选"图片 LLM",开启"文本兜底"<br/>
                  • 标准 KOL：不开启 LLM,使用规则解析
                </div>
              </div>
            )}
          </div>
          <Button className="w-full" onClick={save}>保存</Button>
        </div>
      </Modal>
    </div>
  );
}
