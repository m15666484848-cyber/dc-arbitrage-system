# DC 量化跟单系统(KOL 实时下单)实施方案

## Context(背景与目标)

用户要构建一套"DC 量化"跟单系统:通过 Discord 用户 TOKEN + 频道号监听多个 KOL 的发币策略消息,实时解析信号并自动下单。系统面向多客户使用,每位客户只能看到自己的数据;管理员负责用户/客户/KOL 管理,客户需经时间授权才能使用。要求支持 OKX 测试网 + OKX/Binance/Bybit 实盘,支持马丁格尔/反马丁格尔/普通策略,带持仓、交易记录、信号汇总、KOL 排行、账户走势图,飞书 Webhook 告警,手动平仓/删除订单,信号去重与自动纠错,分批建仓/分批止盈,缺失止盈止损兜底,达到第一止盈或 2% 利润后止损带成本保护。本项目为**全新重起**(不迭代旧 `/home/ubuntu/kol-trader/`),直接上**完整版**。用户已授权我全方面补全未想到的能力。

## 技术选型

- **后端**:Python 3.11 + FastAPI(异步,适合 Discord 监听 + 交易所 WebSocket)+ Uvicorn
- **前端**:React 18 + TypeScript + Vite;UI 用 Tailwind CSS + shadcn/ui(高大上暗色风);图表用 TradingView Lightweight Charts(账户走势/KOL 净值)+ Recharts(统计);状态用 Zustand + TanStack Query;实时用 WebSocket
- **数据库**:PostgreSQL 16(主库,多租户/订单/交易记录)+ Redis 7(实时状态/缓存/限流/信号去重窗口)
- **交易所**:ccxt(统一 OKX/Binance/Bybit,含 OKX 测试网 `sandbox=True`)
- **Discord**:用户 TOKEN 直连 Gateway WebSocket(轻量、自托管;非 discord.py self-bot,降低封号风险)。**注**:用户 TOKEN 属 self-bot,违反 Discord ToS,仅用于监听已加入的付费 KOL 群,已在风险提示中告知用户
- **任务调度**:APScheduler(静默时段/定时对账)+ asyncio 后台 Worker
- **OCR(图片策略)**:本地 Tesseract(免费、离线),可选配置云 OCR 兜底
- **加密**:API Key 用 Fernet 对称加密落库
- **部署**:Docker Compose(跨平台,Windows 开发/Linux 部署通用)
- **认证**:JWT + RBAC(admin/customer)

## 仓库结构(单仓多包)

```
dc-quant/
├── docker-compose.yml
├── .env.example
├── backend/
│   ├── app/
│   │   ├── core/            # config, security(JWT/RBAC/加密), database, redis, logging
│   │   ├── models/          # SQLAlchemy 模型(见下)
│   │   ├── schemas/         # Pydantic 入参/出参
│   │   ├── api/             # REST 路由(auth, admin, trading, strategy, analytics, settings)
│   │   ├── services/        # discord_monitor, signal_parser, signal_filter, strategy_engine,
│   │   │                    # order_manager, position_manager, risk_manager, exchange_adapter,
│   │   │                    # notification, analytics
│   │   ├── workers/         # discord_worker, price_worker, order_monitor, position_monitor
│   │   └── main.py
│   ├── alembic/             # 数据库迁移
│   ├── tests/
│   └── pyproject.toml
└── frontend/
    ├── src/
    │   ├── pages/           # login, dashboard, kols, signals, positions, trades, strategies,
    │   │                    # settings, admin/users, admin/customers, admin/kol-mgmt
    │   ├── components/      # ui(shadcn), charts, layout, tables
    │   ├── api/             # axios + ws client
    │   ├── stores/          # zustand
    │   └── App.tsx
    └── package.json
```

## 数据库模型(关键表)

- `users`:管理员账号
- `customers`:客户账号(登录用);每客户独立多租户隔离
- `authorizations`:时间授权(customer_id, exchange, starts_at, expires_at, active)——未授权或过期则禁止下单
- `kols`:KOL 档(name, discord_channel_id, discord_user_id, enabled, 备注)
- `kol_follows`:客户多选/全选关注的 KOL(customer_id, kol_id, strategy_id)
- `signals`:原始信号(id, kol_id, raw_text, image_url, parsed_json, status, dedup_hash, received_at, corrected)
- `orders`:订单(id, customer_id, signal_id, kol_id, exchange, symbol, side, type, qty, price, status, batch_no, created_at, filled_at, deleted_at)
- `positions`:持仓(id, customer_id, kol_id, exchange, symbol, side, entry_price, qty, tp_levels[JSON], sl, status, cost_protection, breakeven_moved, opened_at)
- `trades`:成交记录(成交流水,用于统计与走势)
- `strategies`:策略配置(customer_id, kol_id, type[normal/martingale/anti_martingale], params, martingale_round, last_result)
- `exchange_accounts`:交易所账号(customer_id, exchange, api_key_enc, api_secret_enc, passphrase_enc, testnet)
- `risk_configs`:静默时段(customer_id, exchange, silent_ranges[JSON], max_position, max_concurrent, max_daily_loss)
- `alerts` + `alert_logs`:飞书 Webhook 配置与发送日志
- `equity_snapshots`:账户净值快照(用于走势图,定时落库)

## 核心业务逻辑设计

### 1. Discord 监听与信号解析
- Gateway WebSocket:IDENTIFY → 心跳 → 监听 `MESSAGE_CREATE`;按 `channel_id` 路由到对应 KOL
- `signal_parser`:
  - 正则提取:符号(`$SOL`/`SOL/USDT`/`SOLUSDT` → 标准化)、方向(long/short/多/空/buy/sell)、入场价、止盈(多级 TP1/TP2/TP3)、止损、杠杆、仓位%
  - 图片走 Tesseract OCR → 复用同一解析管线
  - 符号别名映射表 + 交易所品种校验(无效品种自动跳过并告警)
  - 缺失止盈止损:读策略默认(TP 默认 +10%/+20% 多级,SL 默认 -5%),或"无止损"模式(可配置,高危告警)

### 2. 信号过滤与自动纠错(`signal_filter`)
- **去重**:`dedup_hash = hash(标准化符号 + 方向 + 入场价分桶(±0.5%))`;窗口内同 hash(跨 KOL 或同 KOL)→ 默认保留首个,可配置"合并"或"全部记录不下单"
- **价格纠错**:入场价偏离当前市价 >15% → 判定笔误,自动改为市价并标记 `corrected`;偏离 30%+ 直接丢弃并告警
- **方向纠错**:long 但 TP<入场,或 SL>入场 → 翻转方向并标记;或丢弃告警(可配置)
- **符号纠错**:常见错别字映射(`$SOl`→`SOL`)
- **黑名单**:稳定币对、已下架品种、明显垃圾消息(无符号/无方向)→ 丢弃
- 所有纠错动作落库 `signals.corrected` 与告警,前端"信号汇总"可见处理轨迹

### 3. 策略引擎(`strategy_engine`)
- **普通策略**:按固定仓位下单
- **马丁格尔**:上一单亏损 → 下一单 ×倍数(默认 2x),连胜/连亏上限熔断(默认 3 轮);盈利后重置
- **反马丁格尔**:上一单盈利 → 下一单 ×倍数;亏损后重置
- 每 KOL 独立追踪 `martingale_round` 与 `last_result`
- 静默时段内:信号仅记录不下单(可配置"静默期信号延迟到开盘补单")

### 4. 订单/持仓管理
- **分批建仓**:同 KOL 同符号在窗口内多次入场 → 按"分批建仓"策略合并均摊入场价,或保留子单(可配置)
- **分批止盈**:TP1/TP2/TP3 各平 X%(默认 30/30/40);每级成交后更新剩余仓位
- **成本保护**:达到 TP1 或浮盈 +2% → 止损上移至入场价 + 缓冲(默认 +0.2%),标记 `breakeven_moved`;防盈利单变亏损
- **手动平仓**:客户可一键平指定持仓;走交易所市价反向单
- **删除订单**:仅未成交挂单可删;已成交走平仓;软删除留痕
- **止损移动**:可选追踪止损(trailing)在盈利后跟进
- 每订单/持仓带 `kol_id`、`created_at`、来源 `signal_id`,前端全程可见"这是哪个 KOL 的单、何时下"

### 5. 风控(`risk_manager`)
- 静默时段(多段,如 23:00-07:00):跨时区,按客户配置
- 最大单笔仓位、最大并发持仓数、单日最大亏损(触发即停止跟单并飞书告警)
- 每 KOL 独立资金上限

### 6. 告警(`notification` - 飞书 Webhook)
- 触发点:收到信号、下单成功、止盈/止损成交、纠错发生、风控熔断、授权过期、交易所错误
- 消息卡片含:KOL、符号、方向、价格、时间、客户(管理员视图)

### 7. 分析(`analytics`)
- **KOL 排行**:胜率、总盈亏、信号数、平均收益率、跟单客户数;可按时间段筛选
- **账户走势图**:基于 `equity_snapshots` 的净值曲线(TradingView Lightweight Charts)
- **信号汇总**:所有信号及处理状态(去重/纠错/下单/拒绝)流水
- **交易记录**:成交流水,含 KOL、时间、盈亏;支持删除未成交单

## 权限与多租户

- 角色:`admin`(管理一切)、`customer`(仅自身数据)
- 客户无"用户管理""KOL 设置"入口(前端按角色隐藏;后端 RBAC 中间件强制)
- 客户需有未过期的 `authorizations` 才能下单;过期则只读 + 飞书告警
- 所有客户态查询自动 `WHERE customer_id = current`;管理员可跨客户视图

## 前端(漂亮高大上)

- 暗色主题 + 渐变点缀 + 玻璃拟态卡片;侧边栏导航(角色感知)+ 顶部状态栏(授权状态/连接状态)
- 页面:登录、仪表盘(净值图+关键指标)、KOL 排行、信号汇总、持仓(实时+手动平仓)、交易记录(删除)、策略管理(创建/编辑/绑定 KOL)、设置(交易所账号/静默时段/飞书/交易参数)、管理端(用户/客户授权/KOL 管理)
- WebSocket 实时推送:新信号、持仓更新、成交、告警 toast

## 构建顺序(完整版,分层推进)

1. 脚手架:monorepo + Docker Compose + .env + 后端 FastAPI 骨架 + 前端 Vite 骨架
2. 数据库模型 + Alembic 迁移 + Redis 连接
3. 认证(JWT/RBAC)+ 时间授权中间件 + 加密工具
4. 多租户 API 框架 + 异常处理 + Pydantic schemas
5. Discord Gateway 监听 + 信号解析(文本+OCR)+ 过滤/去重/纠错
6. 交易所适配器(ccxt 三所 + OKX 测试网)+ 加密 API Key + WebSocket 行情
7. 策略引擎 + 订单/持仓管理(分批建仓/分批止盈/成本保护/手动平仓/删除)
8. 风控(静默时段/熔断)+ 飞书告警
9. 分析服务(KOL 排行/净值快照/信号汇总/交易记录)
10. 前端全部页面 + 高端 UI + WebSocket 实时
11. 管理端(用户/客户授权/KOL 管理)
12. 测试 + 部署文档 + .env.example

## 关键文件(代表)

- `backend/app/services/discord_monitor.py`、`signal_parser.py`、`signal_filter.py`
- `backend/app/services/strategy_engine.py`、`order_manager.py`、`position_manager.py`
- `backend/app/services/exchange_adapter.py`、`risk_manager.py`、`notification.py`、`analytics.py`
- `backend/app/api/{auth,admin,trading,strategy,analytics,settings}.py`
- `backend/app/models/*.py`、`backend/app/core/{security,database,config}.py`
- `frontend/src/pages/*.tsx`、`frontend/src/components/charts/*`

## 验证方式

- `docker compose up` 启动 PG+Redis+后端+前端
- 后端:`pytest` 覆盖信号解析/去重/纠错/策略/成本保护单测
- 前端:`pnpm dev` 起开发服,登录 admin 与 customer 两账号验证权限隔离
- OKX 测试网:导入测试 API Key,模拟 KOL 信号(本地 webhook 注入)→ 验证下单/止盈/成本保护/手动平仓
- 飞书:配置 Webhook,触发信号与告警验证送达
- 信号处理:注入重复/错误价格/错误方向/缺 TP/SL 的信号,验证去重/纠错/兜底逻辑与前端"信号汇总"展示

## 额外补全(用户未提但我加上)

- 追踪止损(Trailing Stop)可选开启
- 连亏/连胜熔断(马丁格尔安全阀)
- 交易所断线自动重连与订单状态对账(防止漏单/重复单)
- 信号置信度评分(格式完整度+KOL 历史胜率)排序展示
- 操作审计日志(admin 所有操作留痕)
- 多语言信号解析(中英文 KOL 消息)
- 授权到期前 N 天飞书提前预警
- 健康检查端点 + Docker healthcheck
