# DC 量化跟单系统(KOL 实时下单)

通过 Discord 用户 TOKEN + 频道号监听多个 KOL 的发币策略消息,实时解析信号并自动下单。支持 OKX 测试网 + OKX / Binance / Bybit 实盘,马丁格尔 / 反马丁格尔 / 普通策略,持仓、交易记录、信号汇总、KOL 排行、账户走势图,飞书 Webhook 告警,手动平仓 / 删除订单,信号去重与自动纠错,分批建仓 / 分批止盈,缺失止盈止损兜底,达到第一止盈或 2% 利润后止损带成本保护。多租户:客户只见自身数据,管理员负责用户 / 客户 / KOL 管理与时间授权。

## 技术栈

- 后端:Python 3.11 + FastAPI + SQLAlchemy(async)+ Alembic + Redis + ccxt
- 前端:React 18 + TypeScript + Vite + Tailwind + shadcn/ui + TradingView Lightweight Charts
- 数据库:PostgreSQL 16 + Redis 7
- 部署:Docker Compose

## 快速开始

```bash
cp .env.example .env          # 填写 DISCORD_TOKEN / 密钥等
docker compose up -d --build
# 后端初始化管理员(首次):
docker compose exec backend python -m app.scripts.init_admin
# 访问前端 http://localhost:5173
```

## 风险提示

- Discord 用户 TOKEN 属 self-bot,违反 Discord ToS,仅用于监听已加入的付费 KOL 群,账号有被封风险,责任自负。
- 本系统不构成投资建议,跟单盈亏自负。

详见 `.trae/documents/DC量化跟单系统实施方案.md`。
