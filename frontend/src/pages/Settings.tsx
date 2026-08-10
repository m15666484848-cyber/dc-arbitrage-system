import { useState, useEffect } from "react";
import { Key, Clock, Bell, Plus, Trash2, Gauge, RotateCcw, Pencil, Shield, AlertTriangle, Timer, TrendingDown, FlaskConical, Save, Gift, Copy, UserPlus, Check, Lock } from "lucide-react";
import { API } from "@/api/client";
import { useFetch } from "@/lib/useFetch";
import { useToast } from "@/components/ui/Toast";
import { Card, CardTitle, Badge, Button, Empty, Input, Select, Field } from "@/components/ui";
import { Modal } from "@/components/ui/Modal";

export default function SettingsPage() {
  const [tab, setTab] = useState<"exchange" | "templates" | "risk" | "multiplier" | "alert" | "advanced" | "invite" | "security">("exchange");
  return (
    <div className="space-y-6">
      <div>
        <h1 className="text-xl font-bold gradient-text flex items-center gap-2">交易设置</h1>
        <p className="text-sm text-slate-500 mt-1">交易所账号 · 风控静默时段 · 品种倍率 · 告警查看 · 高级风控 · 邀请</p>
      </div>
      <div className="flex gap-2 flex-wrap">
        {[
          { k: "exchange", label: "交易所账号", icon: Key },
          { k: "templates", label: "推荐模板", icon: Save },
          { k: "risk", label: "风控/静默时段", icon: Clock },
          { k: "multiplier", label: "品种倍率", icon: Gauge },
          { k: "advanced", label: "高级风控", icon: Shield },
          { k: "alert", label: "告警查看", icon: Bell },
          { k: "invite", label: "邀请推广", icon: Gift },
          { k: "security", label: "修改密码", icon: Lock },
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
      {tab === "exchange" && <ExchangeTab />}
      {tab === "templates" && <TemplateTab />}
      {tab === "risk" && <RiskTab />}
      {tab === "multiplier" && <MultiplierTab />}
      {tab === "advanced" && <AdvancedRiskTab />}
      {tab === "alert" && <AlertTab />}
      {tab === "invite" && <InviteTab />}
      {tab === "security" && <SecurityTab />}
    </div>
  );
}

function MultiplierTab() {
  const { data, reload } = useFetch(() => API.getSymbolMultipliers(), []);
  const { data: customData, reload: reloadCustom } = useFetch(() => API.getCustomSymbols(), []);
  const { push } = useToast();
  const list: any[] = data || [];
  const customList: any[] = customData || [];
  const [drafts, setDrafts] = useState<Map<number, number>>(new Map());

  const getMultiplier = (cfg: any) => drafts.get(cfg.id) ?? cfg.multiplier;

  const onChange = (id: number, v: number) => {
    setDrafts((prev) => new Map(prev).set(id, v));
  };

  const save = async () => {
    try {
      const updates: { config_id: number; multiplier: number }[] = [];
      for (const cfg of list) {
        if (drafts.has(cfg.id)) {
          updates.push({ config_id: cfg.id, multiplier: drafts.get(cfg.id)! });
        }
      }
      if (updates.length === 0) {
        push("info", "没有需要保存的修改");
        return;
      }
      await API.setSymbolMultipliers(updates);
      push("success", `已保存 ${updates.length} 个分类倍率`);
      setDrafts(new Map());
      reload();
    } catch (e: any) {
      push("error", e?.response?.data?.message || "保存失败");
    }
  };

  const reset = async (config_id: number) => {
    try {
      await API.resetSymbolMultiplier(config_id);
      setDrafts((prev) => {
        const n = new Map(prev);
        n.delete(config_id);
        return n;
      });
      push("success", "已重置为默认值");
      reload();
    } catch (e: any) {
      push("error", e?.response?.data?.message || "重置失败");
    }
  };

  const hasChanges = drafts.size > 0;

  return (
    <div className="space-y-4">
      <Card>
        <CardTitle action={hasChanges ? <Button onClick={save}>保存修改</Button> : null}>
          品种分类倍率
        </CardTitle>
        <p className="text-xs text-slate-500 mb-4">
          每个品种分类可独立设置跟单金额倍率。最终下单金额 = 策略金额 × 倍率。未设置时使用管理员默认值。
        </p>
        <div className="space-y-3">
          {list.map((cfg) => {
            const current = getMultiplier(cfg);
            const changed = drafts.has(cfg.id);
            return (
              <div key={cfg.id} className="glass-soft p-3 rounded-xl">
                <div className="flex items-center justify-between gap-4">
                  <div className="flex-1 min-w-0">
                    <div className="flex items-center gap-2 flex-wrap">
                      <span className="font-medium text-slate-100">{cfg.name}</span>
                      <div className="flex gap-1 flex-wrap">
                        {cfg.symbols?.split(",").filter(Boolean).map((s: string) => (
                          <Badge key={s} tone="accent" className="!py-0 !px-1.5 !text-[10px]">{s.trim()}</Badge>
                        ))}
                        {!cfg.symbols && <Badge tone="default" className="!py-0 !px-1.5 !text-[10px]">所有其他</Badge>}
                      </div>
                      {cfg.customer_override && !changed && (
                        <Badge tone="accent" className="!py-0 !px-1.5 !text-[10px]">自定义</Badge>
                      )}
                      {changed && (
                        <Badge tone="warn" className="!py-0 !px-1.5 !text-[10px]">待保存</Badge>
                      )}
                    </div>
                    <div className="text-xs text-slate-500 mt-1">{cfg.note || "—"}</div>
                    <div className="text-xs text-slate-600 mt-0.5">
                      默认: <span className="font-mono">{cfg.default_multiplier}x</span>
                      {changed && <span className="text-accent-glow"> → 修改为: <span className="font-mono font-bold">{current}x</span></span>}
                    </div>
                  </div>
                  <div className="flex items-center gap-2">
                    <div className="flex items-center gap-1">
                      <Input
                        type="number"
                        step="0.1"
                        min="0.01"
                        max="100"
                        value={current}
                        onChange={(e) => onChange(cfg.id, parseFloat(e.target.value) || 1.0)}
                        className="w-20 text-center font-mono"
                      />
                      <span className="text-slate-500 text-sm">x</span>
                    </div>
                    {(cfg.customer_override || changed) && (
                      <button
                        onClick={() => reset(cfg.id)}
                        className="p-1.5 text-slate-400 hover:text-accent-glow"
                        title="重置为默认值"
                      >
                        <RotateCcw size={14} />
                      </button>
                    )}
                  </div>
                </div>
              </div>
            );
          })}
        </div>
      </Card>

      <CustomSymbolSection
        list={customList}
        reload={reloadCustom}
        push={push}
      />

      <div className="glass p-4 rounded-xl border border-border-soft">
        <h3 className="text-sm font-semibold text-slate-300 mb-2">计算示例</h3>
        <div className="text-xs text-slate-500 space-y-1">
          <div>策略基础金额: <span className="font-mono text-accent-glow">100 USDT</span></div>
          <div>BTC 信号 (主流币 0.5x): <span className="font-mono text-accent-glow">100 × 0.5 = 50 USDT</span></div>
          <div>DOGE 信号 (山寨币 2.0x): <span className="font-mono text-accent-glow">100 × 2.0 = 200 USDT</span></div>
          <div>XAU 信号 (贵金属 1.0x): <span className="font-mono text-accent-glow">100 × 1.0 = 100 USDT</span></div>
          <div className="text-accent-glow mt-1">SKHY 信号 (自定义 5.0x): <span className="font-mono text-accent-glow">100 × 5.0 = 500 USDT</span></div>
        </div>
      </div>
    </div>
  );
}

function CustomSymbolSection({ list, reload, push }: { list: any[]; reload: () => void; push: any }) {
  const [adding, setAdding] = useState(false);
  const [newSymbol, setNewSymbol] = useState("");
  const [newMultiplier, setNewMultiplier] = useState(1.0);
  const [editingId, setEditingId] = useState<number | null>(null);
  const [editValue, setEditValue] = useState(1.0);

  const add = async () => {
    try {
      if (!newSymbol.trim()) {
        push("info", "请输入币种代码");
        return;
      }
      await API.addCustomSymbol({ symbol: newSymbol.trim().toUpperCase(), multiplier: newMultiplier });
      push("success", `已添加 ${newSymbol.trim().toUpperCase()} 倍率 ${newMultiplier}x`);
      setNewSymbol("");
      setNewMultiplier(1.0);
      setAdding(false);
      reload();
    } catch (e: any) {
      push("error", e?.response?.data?.message || "添加失败");
    }
  };

  const startEdit = (item: any) => {
    setEditingId(item.id);
    setEditValue(item.multiplier);
  };

  const saveEdit = async (id: number) => {
    try {
      await API.updateCustomSymbol(id, { multiplier: editValue });
      push("success", "倍率已更新");
      setEditingId(null);
      reload();
    } catch (e: any) {
      push("error", e?.response?.data?.message || "更新失败");
    }
  };

  const remove = async (id: number) => {
    if (!window.confirm("确认删除此自定义币种倍率?此操作不可撤销")) return;
    try {
      await API.deleteCustomSymbol(id);
      push("success", "已删除自定义币种");
      reload();
    } catch (e: any) {
      push("error", e?.response?.data?.message || "删除失败");
    }
  };

  return (
    <Card>
      <CardTitle action={
        !adding ? (
          <Button onClick={() => setAdding(true)}><Plus size={15} /> 添加自定义币种</Button>
        ) : null
      }>
        自定义币种倍率
      </CardTitle>
      <p className="text-xs text-slate-500 mb-4">
        为特定币种设置独立倍率,优先级高于分类倍率。例如 SKHY 设置 5.0x 后,所有 SKHY 相关信号都会使用此倍率。
      </p>

      {adding && (
        <div className="glass-soft p-3 rounded-xl mb-4 border border-accent/30">
          <div className="flex items-end gap-3 flex-wrap">
            <div className="flex-1 min-w-[140px]">
              <label className="label text-xs">币种代码</label>
              <Input
                value={newSymbol}
                onChange={(e) => setNewSymbol(e.target.value)}
                placeholder="如 SKHY、MU、TSLA"
                className="font-mono uppercase"
                maxLength={20}
              />
            </div>
            <div className="w-28">
              <label className="label text-xs">倍率</label>
              <div className="flex items-center gap-1">
                <Input
                  type="number"
                  step="0.1"
                  min="0.01"
                  max="100"
                  value={newMultiplier}
                  onChange={(e) => setNewMultiplier(parseFloat(e.target.value) || 1.0)}
                  className="font-mono text-center"
                />
                <span className="text-slate-500 text-sm">x</span>
              </div>
            </div>
            <div className="flex gap-2">
              <Button onClick={add}>确认</Button>
              <Button variant="ghost" onClick={() => { setAdding(false); setNewSymbol(""); setNewMultiplier(1.0); }}>取消</Button>
            </div>
          </div>
          <div className="text-xs text-slate-500 mt-2">
            提示:输入币种前缀即可匹配所有相关交易对(如 "SKHY" 匹配 SKHY/USDT、SKHYUSDT 等)
          </div>
        </div>
      )}

      {list.length === 0 ? (
        <Empty text="还没有自定义币种,点击右上角添加" />
      ) : (
        <div className="space-y-2">
          {list.map((item) => (
            <div key={item.id} className="glass-soft p-3 rounded-xl">
              <div className="flex items-center justify-between gap-4">
                <div className="flex items-center gap-3 flex-1 min-w-0">
                  <div className="w-10 h-10 rounded-lg bg-accent/15 flex items-center justify-center text-accent-glow font-bold text-xs">
                    {item.symbol.slice(0, 3)}
                  </div>
                  <div className="flex-1 min-w-0">
                    <div className="flex items-center gap-2">
                      <span className="font-medium text-slate-100 font-mono">{item.symbol}</span>
                      <Badge tone="accent" className="!py-0 !px-1.5 !text-[10px]">自定义</Badge>
                    </div>
                    <div className="text-xs text-slate-500 mt-0.5">
                      匹配 {item.symbol}/USDT 等 {item.symbol} 开头的交易对
                    </div>
                  </div>
                </div>
                <div className="flex items-center gap-2">
                  {editingId === item.id ? (
                    <>
                      <div className="flex items-center gap-1">
                        <Input
                          type="number"
                          step="0.1"
                          min="0.01"
                          max="100"
                          value={editValue}
                          onChange={(e) => setEditValue(parseFloat(e.target.value) || 1.0)}
                          className="w-20 text-center font-mono"
                          autoFocus
                        />
                        <span className="text-slate-500 text-sm">x</span>
                      </div>
                      <button onClick={() => saveEdit(item.id)} className="p-1.5 text-accent-glow hover:text-accent">
                        ✓
                      </button>
                      <button onClick={() => setEditingId(null)} className="p-1.5 text-slate-400 hover:text-slate-200">
                        ✕
                      </button>
                    </>
                  ) : (
                    <>
                      <div className="flex items-center gap-1 mr-1">
                        <span className="font-mono font-bold text-accent-glow">{item.multiplier}</span>
                        <span className="text-slate-500 text-sm">x</span>
                      </div>
                      <button
                        onClick={() => startEdit(item)}
                        className="p-1.5 text-slate-400 hover:text-accent-glow"
                        title="编辑倍率"
                      >
                        <Pencil size={14} />
                      </button>
                      <button
                        onClick={() => remove(item.id)}
                        className="p-1.5 text-slate-400 hover:text-danger"
                        title="删除"
                      >
                        <Trash2 size={14} />
                      </button>
                    </>
                  )}
                </div>
              </div>
            </div>
          ))}
        </div>
      )}
    </Card>
  );
}

function TemplateTab() {
  const templates = [
    {
      name: "主流币稳健",
      badge: "推荐新手",
      tone: "profit" as const,
      risk: "低风险",
      scene: "只跟 BTC / ETH / SOL 等主流币，适合先跑稳定跟单。",
      config: ["单笔金额偏小", "最多 2-3 个持仓", "必须带 TP + SL", "优先默认下单 API"],
    },
    {
      name: "均衡跟单",
      badge: "通用",
      tone: "accent" as const,
      risk: "中风险",
      scene: "主流币和部分高流动性山寨币都允许，适合有一定经验的用户。",
      config: ["开启币种过滤", "限制最大委托数", "挂单超时建议开启", "严格控制单 KOL 暴露"],
    },
    {
      name: "山寨币观察",
      badge: "谨慎",
      tone: "warn" as const,
      risk: "高风险",
      scene: "只作为观察模板，不建议直接大资金运行。",
      config: ["单笔金额降低", "最大仓位数降低", "必须设置止损", "建议先测试网验证"],
    },
    {
      name: "测试网模拟",
      badge: "安全验证",
      tone: "gold" as const,
      risk: "模拟盘",
      scene: "用于验证 API、信号解析、挂单和止盈止损流程。",
      config: ["使用测试网 API", "不影响真实资金", "适合上线前验证", "观察执行时间线"],
    },
  ];

  return (
    <Card>
      <CardTitle action={<Badge tone="gold"><Save size={12} /> 仅展示，不自动套用</Badge>}>
        系统推荐模板
      </CardTitle>
      <div className="mb-4 rounded-xl border border-white/10 bg-white/[0.03] p-3 text-sm text-slate-400 leading-relaxed">
        这里先做“推荐模板预览”，不会覆盖你的真实交易配置。后续如果要做保存/加载模板，再加二次确认，避免误改正在运行的跟单设置。
      </div>
      <div className="grid gap-3 md:grid-cols-2 xl:grid-cols-4">
        {templates.map((tpl) => (
          <div key={tpl.name} className="glass-soft p-4 border border-white/10">
            <div className="flex items-start justify-between gap-2">
              <div>
                <div className="font-semibold text-slate-100">{tpl.name}</div>
                <div className="mt-1 text-xs text-slate-500">{tpl.risk}</div>
              </div>
              <Badge tone={tpl.tone}>{tpl.badge}</Badge>
            </div>
            <div className="mt-3 text-xs text-slate-400 leading-relaxed">{tpl.scene}</div>
            <div className="mt-3 space-y-1.5">
              {tpl.config.map((item) => (
                <div key={item} className="flex items-center gap-2 text-xs text-slate-300">
                  <Check size={12} className="text-accent-glow shrink-0" />
                  <span>{item}</span>
                </div>
              ))}
            </div>
          </div>
        ))}
      </div>
    </Card>
  );
}

function ExchangeTab() {
  const { data, reload } = useFetch(() => API.listExchangeAccounts(), []);
  const { data: strategiesData } = useFetch(() => API.listStrategies(), []);
  const { data: positionsData } = useFetch(() => API.listPositions(), []);
  const { data: pendingData } = useFetch(() => API.listPendingOrders("pending"), []);
  const { data: riskOverviewData, reload: reloadRiskOverview } = useFetch(() => API.getExchangeRiskOverview(), []);
  const { push } = useToast();
  const [modal, setModal] = useState(false);
  const [saving, setSaving] = useState(false);
  const [settingDefault, setSettingDefault] = useState<number | null>(null);
  const [testingId, setTestingId] = useState<number | null>(null);
  const [savingFollowId, setSavingFollowId] = useState<number | null>(null);
  const [followDrafts, setFollowDrafts] = useState<Record<number, any>>({});
  const [refreshingBalanceId, setRefreshingBalanceId] = useState<number | null>(null);
  const [refreshingSummary, setRefreshingSummary] = useState(false);
  const [balances, setBalances] = useState<Record<number, any>>({});
  const [balanceSummary, setBalanceSummary] = useState<any>(null);
  const [copiedIp, setCopiedIp] = useState(false);
  const [f, setF] = useState({
    exchange: "okx",
    label: "",
    api_key: "",
    api_secret: "",
    passphrase: "",
    testnet: false,
    account_mode: "live",
    follow_enabled: false,
    follow_weight: 1,
    max_order_usdt: 0,
    strategy_id: null as number | null,
  });
  const list: any[] = data || [];
  const strategies: any[] = Array.isArray(strategiesData) ? strategiesData : [];
  const positions: any[] = Array.isArray(positionsData) ? positionsData : [];
  const pendingOrders: any[] = Array.isArray(pendingData) ? pendingData : (pendingData?.items || []);
  const riskOverview: any[] = Array.isArray(riskOverviewData) ? riskOverviewData : [];
  const platformWhitelistIp = "43.128.149.246";
  const formatMoney = (v: any) => {
    const n = Number(v || 0);
    return n.toLocaleString(undefined, { maximumFractionDigits: 2 });
  };
  const formatTime = (v?: string | null) => {
    if (!v) return "未验证";
    try {
      return new Date(v).toLocaleString();
    } catch {
      return v;
    }
  };
  const getAccountStatus = (a: any) => {
    const status = a.status || (a.last_error ? "failed" : a.last_verified_at ? "verified" : "unverified");
    if (status === "failed") return { label: "验证失败", tone: "loss" as const };
    if (status === "verified") return { label: "已验证", tone: "profit" as const };
    return { label: "未验证", tone: "warn" as const };
  };
  const exchangeOptions = [
    { value: "okx", label: "OKX", desc: "推荐，合约跟单常用" },
    { value: "binance", label: "Binance", desc: "币安合约账号" },
    { value: "bybit", label: "Bybit", desc: "Bybit 合约账号" },
  ];
  const getAccountMode = (a: any) => a.account_mode || (a.testnet ? "testnet" : "live");
  const getModeLabel = (mode: string) => {
    if (mode === "demo") return "Demo Trading";
    if (mode === "testnet") return "测试网";
    return "实盘";
  };
  const getModeBadgeTone = (mode: string) => {
    if (mode === "live") return "profit" as const;
    if (mode === "demo") return "accent" as const;
    return "warn" as const;
  };
  const setAccountMode = (mode: string) => {
    setF({ ...f, account_mode: mode, testnet: mode !== "live" });
  };
  const exchangeOverview = Object.values(
    list.reduce((acc: Record<string, any>, item: any) => {
      const mode = getAccountMode(item);
      const key = `${item.exchange}-${mode}`;
      if (!acc[key]) {
        acc[key] = {
          key,
          exchange: item.exchange,
          testnet: item.testnet,
          account_mode: mode,
          count: 0,
          defaultAccount: null,
          hasError: false,
          positionCount: 0,
          pendingCount: 0,
          balance: null,
          risk: null,
        };
      }
      acc[key].count += 1;
      if (item.is_default) acc[key].defaultAccount = item;
      if (item.last_error) acc[key].hasError = true;
      return acc;
    }, {})
  );
  for (const group of exchangeOverview as any[]) {
    group.positionCount = positions.filter((p: any) => p.exchange === group.exchange && p.status === "open" && p.parent_id === null).length;
    group.pendingCount = pendingOrders.filter((o: any) => o.exchange === group.exchange).length;
    group.balance = balanceSummary?.groups?.find((b: any) => b.exchange === group.exchange && getAccountMode(b) === group.account_mode) || null;
    group.risk = riskOverview.find((r: any) => r.exchange === group.exchange && getAccountMode(r) === group.account_mode) || null;
  }

  const copyWhitelistIp = async () => {
    try {
      await navigator.clipboard.writeText(platformWhitelistIp);
      setCopiedIp(true);
      push("success", "平台 IP 已复制");
      setTimeout(() => setCopiedIp(false), 1500);
    } catch {
      push("error", "复制失败,请手动复制 IP");
    }
  };

  const add = async () => {
    if (!f.api_key.trim() || !f.api_secret.trim()) {
      push("error", "请填写 API Key 和 API Secret");
      return;
    }
    setSaving(true);
    try {
      await API.addExchangeAccount({ ...f, testnet: f.account_mode !== "live" });
      push("success", "交易所账号已添加(密钥已加密存储)");
      setModal(false);
      setF({ exchange: "okx", label: "", api_key: "", api_secret: "", passphrase: "", testnet: false, account_mode: "live", follow_enabled: false, follow_weight: 1, max_order_usdt: 0, strategy_id: null });
      reload();
    } catch (e: any) {
      push("error", e?.response?.data?.message || "添加失败");
    } finally {
      setSaving(false);
    }
  };

  const saveFollowConfig = async (a: any) => {
    const draft = { ...a, ...(followDrafts[a.id] || {}) };
    setSavingFollowId(a.id);
    try {
      await API.updateExchangeAccountFollow(a.id, {
        follow_enabled: !!draft.follow_enabled,
        follow_weight: Number(draft.follow_weight || 1),
        max_order_usdt: Number(draft.max_order_usdt || 0),
        strategy_id: draft.strategy_id ? Number(draft.strategy_id) : null,
      });
      push("success", "API 跟单配置已保存");
      setFollowDrafts((prev) => {
        const next = { ...prev };
        delete next[a.id];
        return next;
      });
      reload();
    } catch (e: any) {
      push("error", e?.response?.data?.message || "保存跟单配置失败");
    } finally {
      setSavingFollowId(null);
    }
  };

  const getFollowDraft = (a: any) => ({ ...a, ...(followDrafts[a.id] || {}) });

  const updateFollowDraft = (id: number, patch: any) => {
    setFollowDrafts((prev) => ({ ...prev, [id]: { ...(prev[id] || {}), ...patch } }));
  };

  const remove = async (id: number) => {
    const target = list.find((a) => a.id === id);
    const title = `${target?.exchange?.toUpperCase() || "交易所"} ${getModeLabel(getAccountMode(target || {}))} API`;
    const warning = [
      `确认删除 ${title} 吗？`,
      "",
      "删除后：",
      "1. 该 API 将不再用于自动下单、余额读取和连接测试。",
      "2. 如果它是默认下单 API，系统会尝试选择同交易所剩余 API 作为默认；没有剩余 API 时，该交易所将无法继续下单。",
      "3. API 记录会停用，建议确认没有正在依赖该 API 的持仓或挂单后再删除。",
    ].join("\n");
    if (!window.confirm(warning)) return;
    try {
      await API.deleteExchangeAccount(id);
      push("success", "已删除");
      reload();
    } catch (e: any) {
      push("error", e?.response?.data?.message || "删除失败");
    }
  };

  const test = async (id: number) => {
    setTestingId(id);
    try {
      push("info", "正在连接交易所...");
      const result = await API.testExchangeAccount(id);
      if (result?.success) {
        push("success", result?.message || "连接成功");
        setBalances((prev) => ({ ...prev, [id]: result }));
        reload();
        reloadRiskOverview();
      } else {
        push("error", result?.message || "连接失败");
      }
    } catch (e: any) {
      push("error", e?.response?.data?.message || "连接失败,请检查 API Key 和网络");
    } finally {
      setTestingId(null);
    }
  };

  const setDefault = async (id: number) => {
    setSettingDefault(id);
    try {
      await API.setDefaultExchangeAccount(id);
      push("success", "已设为默认下单 API");
      reload();
      reloadRiskOverview();
    } catch (e: any) {
      push("error", e?.response?.data?.message || "设置默认 API 失败");
    } finally {
      setSettingDefault(null);
    }
  };

  const refreshBalance = async (id: number) => {
    setRefreshingBalanceId(id);
    try {
      const result = await API.getExchangeAccountBalance(id);
      setBalances((prev) => ({ ...prev, [id]: result }));
      push("success", "余额已刷新");
      reload();
      reloadRiskOverview();
    } catch (e: any) {
      push("error", e?.response?.data?.message || "余额刷新失败");
      reload();
    } finally {
      setRefreshingBalanceId(null);
    }
  };

  const refreshAllBalances = async () => {
    setRefreshingSummary(true);
    try {
      const result = await API.getExchangeBalanceSummary();
      setBalanceSummary(result);
      const next: Record<number, any> = {};
      for (const item of result?.accounts || []) {
        next[item.id] = item;
      }
      setBalances((prev) => ({ ...prev, ...next }));
      const failed = (result?.accounts || []).filter((item: any) => item.error).length;
      push(failed ? "info" : "success", failed ? `余额已刷新，${failed} 个 API 失败` : "全部余额已刷新");
      reload();
      reloadRiskOverview();
    } catch (e: any) {
      push("error", e?.response?.data?.message || "刷新全部余额失败");
    } finally {
      setRefreshingSummary(false);
    }
  };

  return (
    <Card>
      <CardTitle action={
        <div className="flex gap-2">
          <Button variant="ghost" onClick={refreshAllBalances} disabled={refreshingSummary}>
            <RotateCcw size={15} /> {refreshingSummary ? "刷新中" : "刷新全部余额"}
          </Button>
          <Button onClick={() => setModal(true)}><Plus size={15} /> 导入账号</Button>
        </div>
      }>
        交易所账号(加密存储 / 客户级唯一默认下单 API)
      </CardTitle>
      <div className="mb-4 grid gap-3 lg:grid-cols-[1.1fr_0.9fr]">
        <div className="rounded-xl border border-gold/20 bg-gold/5 p-3 text-xs text-slate-400 leading-relaxed">
          <div className="mb-1 flex items-center gap-2 text-sm font-semibold text-gold">
            <Shield size={14} /> API 安全提示
          </div>
          导入 API 前，请先把平台 IP 加入交易所白名单；API 建议只开启合约交易权限，不要开启提币权限。
        </div>
        <div className="rounded-xl border border-white/10 bg-white/[0.03] p-3">
          <div className="text-xs text-slate-500 mb-2">平台白名单 IP</div>
          <div className="flex items-center gap-2">
            <code className="flex-1 rounded-lg bg-black/20 px-3 py-2 text-sm text-slate-100">{platformWhitelistIp}</code>
            <Button variant="ghost" className="px-3 py-2 text-xs" onClick={copyWhitelistIp}>
              {copiedIp ? <Check size={14} /> : <Copy size={14} />}
              {copiedIp ? "已复制" : "复制"}
            </Button>
          </div>
        </div>
      </div>
      {balanceSummary && (
        <div className="mb-4 grid gap-3 md:grid-cols-3">
          <div className="rounded-xl border border-accent/20 bg-accent/5 p-3">
            <div className="text-xs text-slate-500">总权益</div>
            <div className="mt-1 font-mono text-lg font-semibold text-slate-100">{formatMoney(balanceSummary.total_equity)} USDT</div>
          </div>
          <div className="rounded-xl border border-accent/20 bg-accent/5 p-3">
            <div className="text-xs text-slate-500">总余额</div>
            <div className="mt-1 font-mono text-lg font-semibold text-slate-100">{formatMoney(balanceSummary.total_balance)} USDT</div>
          </div>
          <div className="rounded-xl border border-accent/20 bg-accent/5 p-3">
            <div className="text-xs text-slate-500">最近刷新</div>
            <div className="mt-1 text-sm text-slate-100">{formatTime(balanceSummary.refreshed_at)}</div>
          </div>
        </div>
      )}
      {list.length === 0 ? (
        <Empty text="尚未导入交易所账号,先导入才能下单" />
      ) : (
        <div className="space-y-4">
          <div className="grid gap-3 md:grid-cols-2 xl:grid-cols-3">
            {exchangeOverview.map((g: any) => (
              <div key={g.key} className="rounded-xl border border-white/10 bg-white/[0.035] p-3">
                <div className="flex items-center justify-between gap-2">
                  <div>
                    <div className="font-semibold text-slate-100">{g.exchange.toUpperCase()}</div>
                    <div className="mt-1 text-xs text-slate-500">{getModeLabel(g.account_mode)}</div>
                  </div>
                  <Badge tone={g.hasError ? "loss" : "accent"}>{g.hasError ? "有异常" : "已配置"}</Badge>
                </div>
                <div className="mt-3 grid grid-cols-2 gap-2 text-xs">
                  <div className="rounded-lg bg-black/15 p-2">
                    <div className="text-slate-500">API 数</div>
                    <div className="mt-1 font-mono text-slate-100">{g.count}</div>
                  </div>
                  <div className="rounded-lg bg-black/15 p-2">
                    <div className="text-slate-500">默认下单</div>
                    <div className="mt-1 truncate text-slate-100">{g.defaultAccount?.label || g.defaultAccount?.api_key_mask || "非默认"}</div>
                  </div>
                  <div className="rounded-lg bg-black/15 p-2">
                    <div className="text-slate-500">持仓数</div>
                    <div className="mt-1 font-mono text-slate-100">{g.positionCount}</div>
                  </div>
                  <div className="rounded-lg bg-black/15 p-2">
                    <div className="text-slate-500">挂单数</div>
                    <div className="mt-1 font-mono text-slate-100">{g.pendingCount}</div>
                  </div>
                  <div className="rounded-lg bg-black/15 p-2">
                    <div className="text-slate-500">权益</div>
                    <div className="mt-1 font-mono text-slate-100">{g.balance ? `${formatMoney(g.balance.equity)} USDT` : "未刷新"}</div>
                  </div>
                  <div className="rounded-lg bg-black/15 p-2">
                    <div className="text-slate-500">余额</div>
                    <div className="mt-1 font-mono text-slate-100">{g.balance ? `${formatMoney(g.balance.balance)} USDT` : "未刷新"}</div>
                  </div>
                  <div className="rounded-lg bg-black/15 p-2">
                    <div className="text-slate-500">单笔上限</div>
                    <div className="mt-1 font-mono text-slate-100">{g.risk?.max_position_usdt ? `${formatMoney(g.risk.max_position_usdt)} USDT` : "不限"}</div>
                  </div>
                  <div className="rounded-lg bg-black/15 p-2">
                    <div className="text-slate-500">剩余名额</div>
                    <div className="mt-1 font-mono text-slate-100">{g.risk?.remaining_position_slots === null || g.risk?.remaining_position_slots === undefined ? "不限" : g.risk.remaining_position_slots}</div>
                  </div>
                </div>
              </div>
            ))}
          </div>
          <div className="grid gap-3">
          {list.map((a) => {
            const fd = getFollowDraft(a);
            const followChanged = !!followDrafts[a.id];
            return (
            <div
              key={a.id}
              className={`glass-soft p-4 border transition ${a.is_default ? "border-gold/45 shadow-[0_0_18px_-8px_rgba(240,180,41,0.55)]" : "border-white/10"}`}
            >
              <div className="flex flex-col md:flex-row md:items-center gap-3">
                <div className="flex items-center gap-3 min-w-0 flex-1">
                  <div className="w-12 h-12 rounded-xl bg-accent/15 flex items-center justify-center text-accent-glow font-bold text-sm uppercase shrink-0">
                    {a.exchange.slice(0, 3)}
                  </div>
                  <div className="min-w-0">
                    <div className="flex items-center gap-2 flex-wrap">
                      <span className="font-semibold text-slate-100">{a.exchange.toUpperCase()}</span>
                      <Badge tone={getModeBadgeTone(getAccountMode(a))}>{getModeLabel(getAccountMode(a))}</Badge>
                      {a.is_default && <Badge tone="gold"><Check size={11} /> 默认下单</Badge>}
                      {!a.is_default && <Badge tone="default">备用 API</Badge>}
                      <Badge tone={getAccountStatus(a).tone}>
                        {getAccountStatus(a).label === "验证失败" && <AlertTriangle size={11} />}
                        {getAccountStatus(a).label}
                      </Badge>
                    </div>
                    <div className="mt-1 text-xs text-slate-500">
                      <span>{a.label || "未命名 API"}</span>
                      <span className="mx-2">·</span>
                      <span className="font-mono text-slate-400">{a.api_key_mask}</span>
                    </div>
                    <div className="mt-1 text-xs text-slate-500">
                      最后验证：{formatTime(a.last_verified_at || balances[a.id]?.last_verified_at)}
                    </div>
                    {balances[a.id] && (
                      <div className="mt-2 grid grid-cols-2 gap-2 text-xs">
                        <div className="rounded-lg bg-black/15 p-2">
                          <div className="text-slate-500">权益</div>
                          <div className="mt-1 font-mono text-slate-100">{formatMoney(balances[a.id].equity)} USDT</div>
                        </div>
                        <div className="rounded-lg bg-black/15 p-2">
                          <div className="text-slate-500">余额</div>
                          <div className="mt-1 font-mono text-slate-100">{formatMoney(balances[a.id].balance)} USDT</div>
                        </div>
                      </div>
                    )}
                    {balances[a.id]?.error && (
                      <div className="mt-2 line-clamp-2 text-xs text-loss/90">
                        刷新失败：{balances[a.id].error}
                      </div>
                    )}
                    {a.last_error && (
                      <div className="mt-2 line-clamp-2 text-xs text-loss/90">
                        最近错误：{a.last_error}
                      </div>
                    )}
                    <div className="mt-3 rounded-xl border border-white/10 bg-black/10 p-3">
                      <div className="mb-2 flex items-center justify-between gap-2">
                        <div>
                          <div className="text-sm font-medium text-slate-200">自动跟单配置</div>
                          <div className="text-xs text-slate-500">每个 API 可独立启用、倍率、策略和单笔上限。</div>
                        </div>
                        <label className="flex items-center gap-2 text-xs text-slate-300">
                          <input
                            type="checkbox"
                            className="accent-accent h-4 w-4"
                            checked={!!fd.follow_enabled}
                            onChange={(e) => updateFollowDraft(a.id, { follow_enabled: e.target.checked })}
                          />
                          参与跟单
                        </label>
                      </div>
                      <div className="grid grid-cols-1 gap-2 md:grid-cols-4">
                        <Field label="API 倍率">
                          <Input
                            type="number"
                            min="0"
                            max="100"
                            step="0.1"
                            value={fd.follow_weight ?? 1}
                            onChange={(e) => updateFollowDraft(a.id, { follow_weight: Number(e.target.value) })}
                            className="font-mono"
                          />
                        </Field>
                        <Field label="单笔上限(USDT)">
                          <Input
                            type="number"
                            min="0"
                            step="1"
                            value={fd.max_order_usdt ?? 0}
                            onChange={(e) => updateFollowDraft(a.id, { max_order_usdt: Number(e.target.value) })}
                            className="font-mono"
                          />
                        </Field>
                        <Field label="独立策略">
                          <Select
                            value={fd.strategy_id || ""}
                            onChange={(e) => updateFollowDraft(a.id, { strategy_id: e.target.value ? Number(e.target.value) : null })}
                          >
                            <option value="">沿用 KOL 策略</option>
                            {strategies.map((s) => (
                              <option key={s.id} value={s.id}>{s.name}</option>
                            ))}
                          </Select>
                        </Field>
                        <div className="flex items-end">
                          <Button
                            variant={followChanged ? "primary" : "ghost"}
                            className="w-full text-xs"
                            disabled={savingFollowId === a.id || !followChanged}
                            onClick={() => saveFollowConfig(a)}
                          >
                            <Save size={13} /> {savingFollowId === a.id ? "保存中" : followChanged ? "保存跟单配置" : "已保存"}
                          </Button>
                        </div>
                      </div>
                    </div>
                  </div>
                </div>
                <div className="flex items-center gap-2 md:justify-end">
                  {!a.is_default && (
                    <Button
                      variant="ghost"
                      className="text-xs px-3 py-2"
                      disabled={settingDefault === a.id}
                      onClick={() => setDefault(a.id)}
                    >
                      {settingDefault === a.id ? "设置中" : "设为默认"}
                    </Button>
                  )}
                  <Button
                    variant="ghost"
                    className="text-xs px-3 py-2"
                    disabled={refreshingBalanceId === a.id}
                    onClick={() => refreshBalance(a.id)}
                  >
                    <RotateCcw size={13} /> {refreshingBalanceId === a.id ? "刷新中" : "刷新余额"}
                  </Button>
                  <button onClick={() => test(a.id)} className="text-slate-400 hover:text-accent-glow p-2" title="测试连接" disabled={testingId === a.id}>
                    {testingId === a.id ? (
                      <span className="text-xs text-accent-glow">测试中</span>
                    ) : (
                      <svg width="16" height="16" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2" strokeLinecap="round" strokeLinejoin="round"><path d="M22 12h-4l-3 9L9 3l-3 9H2"/></svg>
                    )}
                  </button>
                  <button onClick={() => remove(a.id)} className="text-slate-400 hover:text-loss p-2" title="删除">
                    <Trash2 size={16} />
                  </button>
                </div>
              </div>
            </div>
          );
          })}
          </div>
        </div>
      )}
      <Modal open={modal} onClose={() => setModal(false)} title="导入交易所账号" width="max-w-4xl md:min-w-[760px] xl:min-w-[900px]">
        <div className="space-y-5 min-w-0">
          <div>
            <div className="label">选择交易所</div>
            <div className="grid grid-cols-1 sm:grid-cols-3 gap-2">
              {exchangeOptions.map((opt) => (
                <button
                  key={opt.value}
                  type="button"
                  onClick={() => setF({ ...f, exchange: opt.value, account_mode: f.account_mode === "demo" && opt.value !== "bybit" ? "live" : f.account_mode, testnet: f.account_mode !== "live" && !(f.account_mode === "demo" && opt.value !== "bybit") })}
                  className={`text-left rounded-xl border p-3 transition min-h-[82px] ${f.exchange === opt.value ? "border-gold/60 bg-gold/10 text-gold" : "border-white/10 bg-white/[0.03] text-slate-300 hover:border-border-soft"}`}
                >
                  <div className="font-semibold">{opt.label}</div>
                  <div className="text-xs text-slate-500 mt-1">{opt.desc}</div>
                </button>
              ))}
            </div>
          </div>
          <Field label="备注标签">
            <Input
              value={f.label}
              placeholder="例如 主账号 / 子账号A / 高频账号"
              onChange={(e) => setF({ ...f, label: e.target.value })}
            />
          </Field>
          <div className="grid grid-cols-1 lg:grid-cols-2 gap-3">
            <Field label="API Key">
              <Input value={f.api_key} autoComplete="off" onChange={(e) => setF({ ...f, api_key: e.target.value })} />
            </Field>
            <Field label="API Secret">
              <Input type="password" autoComplete="new-password" value={f.api_secret} onChange={(e) => setF({ ...f, api_secret: e.target.value })} />
            </Field>
          </div>
          {f.exchange === "okx" && (
            <Field label="Passphrase(OKX 专用)">
              <Input type="password" autoComplete="new-password" value={f.passphrase} onChange={(e) => setF({ ...f, passphrase: e.target.value })} />
            </Field>
          )}
          <Field label="交易环境">
            <Select value={f.account_mode} onChange={(e) => setAccountMode(e.target.value)}>
              <option value="live">实盘</option>
              <option value="testnet">测试网</option>
              {(f.exchange === "bybit" || f.exchange === "binance") && <option value="demo">Demo Trading</option>}
            </Select>
          </Field>
          <div className="text-xs text-slate-500 rounded-xl border border-white/10 bg-white/[0.03] p-3">
            {f.exchange === "bybit"
              ? "Bybit 测试网使用 testnet.bybit.com 的 API；Demo Trading 使用主网内模拟交易 API，二者不能混用。"
              : f.exchange === "binance"
                ? "Binance 测试网使用 testnet.binancefuture.com 的 U 本位合约 API；Demo Trading 使用 demo.binance.com 创建的 API，二者不能混用。"
              : "测试网需要使用对应交易所测试环境创建的 API，不能填写实盘 API。"}
          </div>
          <div className="rounded-xl border border-white/10 bg-white/[0.03] p-3">
            <div className="mb-3 flex items-center justify-between gap-3">
              <div>
                <div className="text-sm font-medium text-slate-200">初始跟单配置</div>
                <div className="text-xs text-slate-500">导入后仍可在账号卡片里单独修改。</div>
              </div>
              <label className="flex items-center gap-2 text-xs text-slate-300">
                <input type="checkbox" className="accent-accent h-4 w-4" checked={f.follow_enabled} onChange={(e) => setF({ ...f, follow_enabled: e.target.checked })} />
                参与自动跟单
              </label>
            </div>
            <div className="grid grid-cols-1 gap-3 md:grid-cols-3">
              <Field label="API 倍率">
                <Input type="number" min="0" max="100" step="0.1" value={f.follow_weight} onChange={(e) => setF({ ...f, follow_weight: Number(e.target.value) })} />
              </Field>
              <Field label="单笔上限(USDT,0=不限)">
                <Input type="number" min="0" step="1" value={f.max_order_usdt} onChange={(e) => setF({ ...f, max_order_usdt: Number(e.target.value) })} />
              </Field>
              <Field label="独立策略">
                <Select value={f.strategy_id || ""} onChange={(e) => setF({ ...f, strategy_id: e.target.value ? Number(e.target.value) : null })}>
                  <option value="">沿用 KOL 策略</option>
                  {strategies.map((s) => (
                    <option key={s.id} value={s.id}>{s.name}</option>
                  ))}
                </Select>
              </Field>
            </div>
          </div>
          <div className="text-xs text-slate-500 bg-warn/10 border border-warn/20 rounded-lg p-3">
            提示:API Key 使用 Fernet 加密存储,仅在下单时解密。开启“参与自动跟单”的 API 会独立跟单；没有开启任何 API 时,系统回退使用默认下单 API。
          </div>
          <Button className="w-full min-h-[44px]" onClick={add} disabled={saving}>
            {saving ? "导入中..." : "导入账号"}
          </Button>
        </div>
      </Modal>
    </Card>
  );
}

function RiskTab() {
  const { data, reload } = useFetch(() => API.getRiskConfig(), []);
  const { push } = useToast();
  const [f, setF] = useState<any>({
    exchange: "all", silent_ranges: [{ start: "23:00", end: "07:00" }], silent_action: "ignore",
    max_position_usdt: 0, max_concurrent_positions: 0, max_daily_loss_pct: 10, per_kol_max_usdt: 0, enabled: true,
  });
  const [loaded, setLoaded] = useState(false);

  // 同步后端数据到本地 state(仅首次加载,避免覆盖用户未保存的编辑)
  useEffect(() => {
    if (data && !loaded) {
      const server = Array.isArray(data) ? data[0] : data;
      if (server) {
        setF((prev: any) => ({ ...prev, ...server }));
      }
      setLoaded(true);
    }
  }, [data, loaded]);

  const updateSilentRange = (i: number, key: "start" | "end", val: string) => {
    const n = [...f.silent_ranges];
    n[i] = { ...n[i], [key]: val };
    setF({ ...f, silent_ranges: n });
  };

  const save = async () => {
    try {
      await API.upsertRiskConfig(f);
      push("success", "风控配置已保存");
      reload();
    } catch (e: any) {
      push("error", e?.response?.data?.message || "保存失败");
    }
  };

  return (
    <Card>
      <CardTitle>风控与静默时段</CardTitle>
      <div className="space-y-4">
        <Field label="适用交易所">
          <Select value={f.exchange} onChange={(e) => setF({ ...f, exchange: e.target.value })}>
            <option value="all">全部交易所</option>
            <option value="okx">仅 OKX</option>
            <option value="binance">仅 Binance</option>
            <option value="bybit">仅 Bybit</option>
          </Select>
        </Field>
        <div>
          <label className="label">静默时段(该时段信号仅记录不下单)</label>
          <div className="space-y-2">
            {f.silent_ranges.map((r: any, i: number) => (
              <div key={i} className="flex gap-2 items-center">
                <Input type="time" value={r.start} onChange={(e) => updateSilentRange(i, "start", e.target.value)} className="w-40" />
                <span className="text-slate-500">至</span>
                <Input type="time" value={r.end} onChange={(e) => updateSilentRange(i, "end", e.target.value)} className="w-40" />
                <button onClick={() => setF({ ...f, silent_ranges: f.silent_ranges.filter((_: any, j: number) => j !== i) })} className="text-slate-400 hover:text-loss"><Trash2 size={15} /></button>
              </div>
            ))}
            <Button variant="ghost" className="text-xs" onClick={() => setF({ ...f, silent_ranges: [...f.silent_ranges, { start: "00:00", end: "00:00" }] })}>
              <Plus size={13} /> 添加时段
            </Button>
          </div>
        </div>
        <Field label="静默期处理方式">
          <Select value={f.silent_action} onChange={(e) => setF({ ...f, silent_action: e.target.value })}>
            <option value="ignore">忽略(不下单)</option>
            <option value="log_only">仅记录</option>
            <option value="delay">延迟到开盘补单</option>
          </Select>
        </Field>
        <div className="grid grid-cols-2 gap-3">
          <Field label="单笔最大仓位(USDT,0=不限)"><Input type="number" value={f.max_position_usdt} onChange={(e) => setF({ ...f, max_position_usdt: Number(e.target.value) })} /></Field>
          <Field label="最大并发持仓数(0=不限)"><Input type="number" value={f.max_concurrent_positions} onChange={(e) => setF({ ...f, max_concurrent_positions: Number(e.target.value) })} /></Field>
          <Field label="单日最大亏损(% ,0=不限)"><Input type="number" value={f.max_daily_loss_pct} onChange={(e) => setF({ ...f, max_daily_loss_pct: Number(e.target.value) })} /></Field>
          <Field label="单 KOL 最大仓位(USDT,0=不限)"><Input type="number" value={f.per_kol_max_usdt} onChange={(e) => setF({ ...f, per_kol_max_usdt: Number(e.target.value) })} /></Field>
        </div>
        <Button onClick={save}>保存风控配置</Button>
      </div>
    </Card>
  );
}

function AdvancedRiskTab() {
  const { data, reload } = useFetch(() => API.getRiskConfig(), []);
  const { push } = useToast();
  const [f, setF] = useState<any>({
    exchange: "all",
    silent_ranges: [],
    silent_action: "ignore",
    max_position_usdt: 0,
    max_concurrent_positions: 0,
    max_daily_loss_pct: 10,
    per_kol_max_usdt: 0,
    enabled: true,
    position_timeout_hours: 72,
    consecutive_loss_threshold: 3,
    consecutive_loss_pause_hours: 24,
    kol_frequency_per_hour: 20,
    auto_stop_loss_pct: 5.0,
    enable_trailing_stop: false,
    trailing_callback_pct: 1.0,
  });
  const [loaded, setLoaded] = useState(false);

  // 同步后端数据到本地 state(useEffect,避免渲染期间 setState 警告)
  useEffect(() => {
    if (data && !loaded) {
      const server = Array.isArray(data) ? data[0] : data;
      if (server) {
        setF((prev: any) => ({ ...prev, ...server }));
      }
      setLoaded(true);
    }
  }, [data, loaded]);

  const update = (k: string, v: any) => setF({ ...f, [k]: v });

  const save = async () => {
    try {
      await API.upsertRiskConfig(f);
      push("success", "高级风控配置已保存");
      reload();
    } catch (e: any) {
      push("error", e?.response?.data?.message || "保存失败");
    }
  };

  return (
    <div className="space-y-4">
      {/* 持仓超时自动平仓 */}
      <Card>
        <CardTitle>
          <div className="flex items-center gap-2">
            <Timer size={16} className="text-accent-glow" />
            <span>持仓超时自动平仓</span>
          </div>
        </CardTitle>
        <div className="space-y-3">
          <p className="text-xs text-slate-500">
            KOL 发出开仓信号后,如果长时间未补止盈止损,持仓超过设定时长后自动平仓,防止资金长期占用。
          </p>
          <div className="glass-soft p-3 rounded-xl">
            <div className="flex items-center justify-between gap-4">
              <div className="flex-1">
                <div className="text-sm text-slate-200">持仓超时时间</div>
                <div className="text-xs text-slate-500 mt-0.5">
                  0 = 禁用此功能。推荐 24-72 小时。
                </div>
              </div>
              <div className="flex items-center gap-2">
                <Input
                  type="number"
                  min="0"
                  max="240"
                  value={f.position_timeout_hours}
                  onChange={(e) => update("position_timeout_hours", Number(e.target.value))}
                  className="w-24 text-center font-mono"
                />
                <span className="text-slate-500 text-sm">小时</span>
              </div>
            </div>
            <div className="flex gap-2 mt-2">
              {[0, 24, 48, 72].map((h) => (
                <button
                  key={h}
                  onClick={() => update("position_timeout_hours", h)}
                  className={`text-xs px-2 py-1 rounded ${
                    f.position_timeout_hours === h
                      ? "bg-accent text-white"
                      : "bg-bg-hover text-slate-400"
                  }`}
                >
                  {h === 0 ? "禁用" : `${h}h`}
                </button>
              ))}
            </div>
          </div>
        </div>
      </Card>

      {/* 连亏暂停风控 */}
      <Card>
        <CardTitle>
          <div className="flex items-center gap-2">
            <TrendingDown size={16} className="text-loss" />
            <span>KOL 连亏暂停风控</span>
          </div>
        </CardTitle>
        <div className="space-y-3">
          <p className="text-xs text-slate-500">
            某个 KOL 连续亏损达到设定次数后,自动暂停跟随该 KOL,保护资金。暂停到期后自动恢复。
          </p>
          <div className="grid grid-cols-2 gap-3">
            <div className="glass-soft p-3 rounded-xl">
              <div className="text-sm text-slate-200">连亏暂停阈值</div>
              <div className="text-xs text-slate-500 mt-0.5">0 = 禁用</div>
              <div className="flex items-center gap-2 mt-2">
                <Input
                  type="number"
                  min="0"
                  max="20"
                  value={f.consecutive_loss_threshold}
                  onChange={(e) => update("consecutive_loss_threshold", Number(e.target.value))}
                  className="w-20 text-center font-mono"
                />
                <span className="text-slate-500 text-sm">次</span>
              </div>
            </div>
            <div className="glass-soft p-3 rounded-xl">
              <div className="text-sm text-slate-200">暂停时长</div>
              <div className="text-xs text-slate-500 mt-0.5">到期自动恢复</div>
              <div className="flex items-center gap-2 mt-2">
                <Input
                  type="number"
                  min="1"
                  max="168"
                  value={f.consecutive_loss_pause_hours}
                  onChange={(e) => update("consecutive_loss_pause_hours", Number(e.target.value))}
                  className="w-20 text-center font-mono"
                />
                <span className="text-slate-500 text-sm">小时</span>
              </div>
            </div>
          </div>
        </div>
      </Card>

      {/* KOL 频率限制 */}
      <Card>
        <CardTitle>
          <div className="flex items-center gap-2">
            <AlertTriangle size={16} className="text-warn" />
            <span>KOL 信号频率限制</span>
          </div>
        </CardTitle>
        <div className="space-y-3">
          <p className="text-xs text-slate-500">
            防止 KOL 异常频繁发信号导致疯狂下单。单 KOL 每小时信号数超过限制时记录告警。
          </p>
          <div className="glass-soft p-3 rounded-xl">
            <div className="flex items-center justify-between gap-4">
              <div className="flex-1">
                <div className="text-sm text-slate-200">每小时信号上限</div>
                <div className="text-xs text-slate-500 mt-0.5">0 = 禁用。推荐 20-50。</div>
              </div>
              <div className="flex items-center gap-2">
                <Input
                  type="number"
                  min="0"
                  max="200"
                  value={f.kol_frequency_per_hour}
                  onChange={(e) => update("kol_frequency_per_hour", Number(e.target.value))}
                  className="w-24 text-center font-mono"
                />
                <span className="text-slate-500 text-sm">个/小时</span>
              </div>
            </div>
          </div>
        </div>
      </Card>

      {/* 自动止损补充 */}
      <Card>
        <CardTitle>
          <div className="flex items-center gap-2">
            <Shield size={16} className="text-accent-glow" />
            <span>自动止损补充</span>
          </div>
        </CardTitle>
        <div className="space-y-3">
          <p className="text-xs text-slate-500">
            KOL 未设置止损的持仓,系统自动按入场价下跌/上涨一定百分比补充止损,防止裸奔仓位。
          </p>
          <div className="glass-soft p-3 rounded-xl">
            <div className="flex items-center justify-between gap-4">
              <div className="flex-1">
                <div className="text-sm text-slate-200">默认止损百分比</div>
                <div className="text-xs text-slate-500 mt-0.5">
                  0 = 禁用。多单止损 = 入场价 × (1 - %);空单相反。
                </div>
              </div>
              <div className="flex items-center gap-2">
                <Input
                  type="number"
                  step="0.5"
                  min="0"
                  max="50"
                  value={f.auto_stop_loss_pct}
                  onChange={(e) => update("auto_stop_loss_pct", Number(e.target.value))}
                  className="w-24 text-center font-mono"
                />
                <span className="text-slate-500 text-sm">%</span>
              </div>
            </div>
            <div className="flex gap-2 mt-2">
              {[0, 3, 5, 8, 10].map((p) => (
                <button
                  key={p}
                  onClick={() => update("auto_stop_loss_pct", p)}
                  className={`text-xs px-2 py-1 rounded ${
                    f.auto_stop_loss_pct === p
                      ? "bg-accent text-white"
                      : "bg-bg-hover text-slate-400"
                  }`}
                >
                  {p === 0 ? "禁用" : `${p}%`}
                </button>
              ))}
            </div>
          </div>
        </div>
      </Card>

      {/* 追踪止损 */}
      <Card>
        <CardTitle>
          <div className="flex items-center gap-2">
            <RotateCcw size={16} className="text-accent-glow" />
            <span>追踪止损(Trailing Stop)</span>
          </div>
        </CardTitle>
        <div className="space-y-3">
          <p className="text-xs text-slate-500">
            盈利时自动上移止损价,锁定利润。价格回撤超过设定比例时触发平仓。
          </p>
          <label className="flex items-center gap-2 text-sm text-slate-300 glass-soft p-3 rounded-xl cursor-pointer">
            <input
              type="checkbox"
              className="accent-accent w-4 h-4"
              checked={f.enable_trailing_stop}
              onChange={(e) => update("enable_trailing_stop", e.target.checked)}
            />
            <span>启用追踪止损</span>
          </label>
          {f.enable_trailing_stop && (
            <div className="glass-soft p-3 rounded-xl">
              <div className="flex items-center justify-between gap-4">
                <div className="flex-1">
                  <div className="text-sm text-slate-200">回撤触发比例</div>
                  <div className="text-xs text-slate-500 mt-0.5">
                    从最高盈利点回撤此比例时平仓。推荐 0.5%-3%。
                  </div>
                </div>
                <div className="flex items-center gap-2">
                  <Input
                    type="number"
                    step="0.1"
                    min="0.1"
                    max="20"
                    value={f.trailing_callback_pct}
                    onChange={(e) => update("trailing_callback_pct", Number(e.target.value))}
                    className="w-24 text-center font-mono"
                  />
                  <span className="text-slate-500 text-sm">%</span>
                </div>
              </div>
            </div>
          )}
        </div>
      </Card>

      <div className="flex justify-end gap-2">
        <Button variant="ghost" onClick={reload}>重置</Button>
        <Button onClick={save}>
          <Shield size={15} /> 保存高级风控配置
        </Button>
      </div>
    </div>
  );
}

function AlertTab() {
  const { data, reload } = useFetch(() => API.listAlerts(), []);
  const list: any[] = (data || []).filter((x: any) => x.customer_id !== null);

  return (
    <Card>
      <CardTitle>
        <div className="flex items-center gap-2">
          <Bell size={18} className="text-accent-glow" />
          <span>飞书告警配置</span>
        </div>
      </CardTitle>

      <div className="text-xs text-slate-400 p-3 glass-soft rounded-lg mb-3">
        告警配置由管理员统一设置和管理。如有需要,请联系管理员调整。
      </div>

      {list.length === 0 ? (
        <Empty text="管理员尚未为您配置告警" />
      ) : (
        <div className="space-y-2">
          {list.map((a) => (
            <div key={a.id} className="glass-soft p-3 rounded-lg">
              <div className="flex items-center gap-3 mb-2">
                <Bell size={16} className="text-accent-glow" />
                <div className="flex-1 min-w-0">
                  <div className="text-sm font-medium text-slate-100">{a.name}</div>
                  <div className="text-xs text-slate-500 font-mono truncate">{a.webhook_url}</div>
                </div>
                <Badge tone={a.enabled ? "profit" : "default"}>{a.enabled ? "启用" : "停用"}</Badge>
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

      <div className="mt-3 text-xs text-slate-500">
        <p>当前共有 {list.length} 条告警配置{list.filter((a) => a.enabled).length > 0 ? `,${list.filter((a) => a.enabled).length} 条已启用` : ""}。</p>
      </div>
    </Card>
  );
}

function InviteTab() {
  const { data: me, reload: reloadMe } = useFetch(() => API.me(), []);
  const { data: invitees, reload: reloadInvitees } = useFetch(() => API.getMyInvitees(), []);
  const { push } = useToast();
  const [copiedField, setCopiedField] = useState<string>("");

  const inviteCode = (me as any)?.invite_code || "";
  const invitePath = (me as any)?.invite_link || "";
  const inviterName = (me as any)?.inviter_name || "";
  const inviteeList: any[] = invitees || [];

  const fullInviteLink = invitePath
    ? (invitePath.startsWith("http") ? invitePath : `${window.location.origin}${invitePath}`)
    : "";

  const copyToClipboard = async (text: string, field: string, label: string) => {
    try {
      await navigator.clipboard?.writeText(text);
      setCopiedField(field);
      push("success", `${label}已复制`);
      setTimeout(() => setCopiedField(""), 2000);
    } catch {
      push("error", "复制失败，请手动复制");
    }
  };

  return (
    <div className="space-y-4">
      {/* 邀请码与邀请链接 */}
      <Card>
        <CardTitle>
          <div className="flex items-center gap-2">
            <Gift size={18} className="text-accent-glow" />
            <span>我的邀请码</span>
          </div>
        </CardTitle>

        <div className="text-xs text-slate-400 p-3 glass-soft rounded-lg mb-4">
          分享您的邀请码或邀请链接给好友，好友注册时填写邀请码即可成为您的下级。
          您将自动获得下级正利润的 <span className="text-accent-glow font-bold">10%</span> 佣金提成。
        </div>

        {/* 邀请码 */}
        <div className="glass-soft p-4 rounded-xl mb-3">
          <div className="flex items-center justify-between gap-3">
            <div className="flex-1 min-w-0">
              <div className="text-xs text-slate-500 mb-1">邀请码</div>
              <div className="text-2xl font-bold font-mono text-accent-glow tracking-wider">{inviteCode || "—"}</div>
            </div>
            {inviteCode && (
              <button
                onClick={() => copyToClipboard(inviteCode, "code", "邀请码")}
                className={`px-4 py-2 rounded-lg text-sm font-medium flex items-center gap-2 transition ${
                  copiedField === "code"
                    ? "bg-profit/15 text-profit"
                    : "bg-accent/15 text-accent-glow hover:bg-accent/25"
                }`}
              >
                {copiedField === "code" ? <Check size={15} /> : <Copy size={15} />}
                {copiedField === "code" ? "已复制" : "复制"}
              </button>
            )}
          </div>
        </div>

        {/* 邀请链接 */}
        {fullInviteLink && (
          <div className="glass-soft p-4 rounded-xl">
            <div className="flex items-center justify-between gap-3">
              <div className="flex-1 min-w-0">
                <div className="text-xs text-slate-500 mb-1">邀请链接</div>
                <div className="text-sm font-mono text-slate-200 truncate">{fullInviteLink}</div>
              </div>
              <button
                onClick={() => copyToClipboard(fullInviteLink, "link", "邀请链接")}
                className={`px-4 py-2 rounded-lg text-sm font-medium flex items-center gap-2 transition shrink-0 ${
                  copiedField === "link"
                    ? "bg-profit/15 text-profit"
                    : "bg-accent/15 text-accent-glow hover:bg-accent/25"
                }`}
              >
                {copiedField === "link" ? <Check size={15} /> : <Copy size={15} />}
                {copiedField === "link" ? "已复制" : "复制"}
              </button>
            </div>
          </div>
        )}

        {/* 邀请人信息 */}
        {inviterName && (
          <div className="mt-3 glass-soft p-3 rounded-lg flex items-center gap-2 text-xs text-slate-400">
            <UserPlus size={14} className="text-slate-500" />
            您的邀请人：<span className="text-slate-200 font-medium">{inviterName}</span>
          </div>
        )}
      </Card>

      {/* 邀请的下级列表 */}
      <Card>
        <CardTitle>
          <div className="flex items-center gap-2">
            <UserPlus size={18} className="text-accent-glow" />
            <span>我邀请的用户</span>
          </div>
        </CardTitle>

        {inviteeList.length === 0 ? (
          <Empty text="您还没有邀请任何用户，快去分享邀请码吧！" />
        ) : (
          <div className="space-y-2">
            {inviteeList.map((inv) => (
              <div key={inv.id} className="glass-soft p-3 rounded-lg flex items-center gap-3">
                <div className="w-9 h-9 rounded-lg bg-accent/15 flex items-center justify-center text-accent-glow font-bold text-xs">
                  {(inv.username || "?").slice(0, 2).toUpperCase()}
                </div>
                <div className="flex-1 min-w-0">
                  <div className="flex items-center gap-2">
                    <span className="font-medium text-slate-100">{inv.username}</span>
                    {inv.status === "pending" && <Badge tone="warn">待审批</Badge>}
                    {inv.status === "active" && <Badge tone="profit">已通过</Badge>}
                    {inv.status === "rejected" && <Badge tone="loss">已拒绝</Badge>}
                  </div>
                  <div className="text-xs text-slate-500 mt-0.5">
                    {inv.display_name || "—"} · {inv.created_at ? new Date(inv.created_at).toLocaleDateString() : "—"}
                  </div>
                </div>
                <Badge tone={inv.is_active ? "profit" : "default"}>
                  {inv.is_active ? "激活" : "停用"}
                </Badge>
              </div>
            ))}
          </div>
        )}

        {inviteeList.length > 0 && (
          <div className="mt-3 text-xs text-slate-500 text-center">
            共邀请 {inviteeList.length} 人，其中 {inviteeList.filter((i) => i.status === "active").length} 人已通过审批
          </div>
        )}
      </Card>
    </div>
  );
}


// =============== 修改密码 ===============
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
          <span>修改登录密码</span>
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
            placeholder="至少 6 位"
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
        <Button onClick={submit} disabled={loading}>
          {loading ? "提交中..." : "确认修改"}
        </Button>
      </div>
    </Card>
  );
}
