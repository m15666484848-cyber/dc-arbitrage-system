# DC 量化跟单系统 - 部署指南

## 一、环境要求

- Docker 24+ 与 Docker Compose v2
- 或本地:Python 3.11+、Node 20+、PostgreSQL 16、Redis 7

## 二、快速启动(Docker Compose,推荐)

```bash
# 1. 克隆/进入项目目录
cd dc-quant

# 2. 配置环境变量(务必修改密钥)
cp .env.example .env
# 编辑 .env,填写:
#   JWT_SECRET        → python -c "import secrets; print(secrets.token_urlsafe(48))"
#   FERNET_KEY        → python -c "from cryptography.fernet import Fernet; print(Fernet.generate_key().decode())"
#   ADMIN_USERNAME / ADMIN_PASSWORD
#   DISCORD_TOKEN     → Discord 用户 TOKEN(自助获取,风险自担)

# 3. 构建并启动
docker compose up -d --build

# 4. 首次初始化(也可省略,后端启动时自动建表+建管理员)
docker compose exec backend python -m app.scripts.init_admin

# 5. 访问
#   前端:  http://localhost:5173
#   接口:  http://localhost:8000/api/health
```

## 三、配置说明(.env 关键项)

| 变量 | 说明 |
|------|------|
| `DISCORD_TOKEN` | Discord 用户 TOKEN,监听 KOL 频道用(self-bot,违反 ToS,风险自担) |
| `FERNET_KEY` | 加密交易所 API Key 的密钥,生成后**勿改**(改了无法解密旧密钥) |
| `JWT_SECRET` | JWT 签名密钥 |
| `OCR_ENABLED` | 是否启用图片策略 OCR(Tesseract) |
| `ADMIN_USERNAME/PASSWORD` | 默认管理员账号 |

## 四、OKX 测试网接入(推荐先用测试网验证)

1. 前往 OKX 测试网申请 API Key(合约交易权限)。
2. 管理员登录 → 新建客户 → 授予时间授权(exchange 选 all 或 okx)。
3. 客户登录 → 交易设置 → 交易所账号 → 导入(勾选「测试网」)。
4. 管理员 → KOL 管理 → 添加 KOL(填 Discord 频道号)。
5. 管理员 → 信号监控 → 「注入测试信号」模拟一条 KOL 消息,验证完整链路:
   解析 → 过滤/纠错 → 对关注该 KOL 的客户下单 → 止盈/成本保护 → 飞书告警。

## 五、Discord TOKEN 获取与频道号

- TOKEN:Discord 网页端登录后,开发者工具 → Network 任意请求的 `Authorization` 头。
- 频道号:右键频道 → 复制 ID(需开启开发者模式)。
- KOL 管理:填频道号;若只跟该频道某用户,再填用户 ID,否则留空监听频道所有人。

> ⚠️ 用户 TOKEN 属 self-bot,可能被 Discord 封号,仅用于监听已加入的付费 KOL 群,责任自负。

## 六、本地开发

```bash
# 后端
cd backend
pip install -e ".[dev]"
uvicorn app.main:app --reload --port 8000

# 前端
cd frontend
npm install
npm run dev   # http://localhost:5173,已代理 /api 与 /ws 到 8000
```

需本地 PostgreSQL 与 Redis,或仅起这两个服务:`docker compose up -d postgres redis`。

## 七、数据库迁移

开发期建表由应用启动自动完成(`Base.metadata.create_all`)。生产环境推荐 Alembic:

```bash
cd backend
# 生成迁移
alembic revision --autogenerate -m "init"
# 应用迁移
alembic upgrade head
```

## 八、测试

```bash
cd backend
pytest -v        # 33 个单元测试:信号解析/过滤纠错/策略引擎/去重/静默时段
```

## 九、生产部署注意

- 反向代理(Nginx/Caddy)终结 TLS,WebSocket 需 `Upgrade` 头透传(参考 `frontend/nginx.conf`)。
- `FERNET_KEY`/`JWT_SECRET` 用密钥管理,不要进 Git。
- 定期备份 PostgreSQL(`pgdata` 卷)。
- 飞书 Webhook 建议配独立群,按事件开关筛选告警。
- 交易所 API Key 建议仅开合约交易权限并绑定服务器 IP。

## 十、健康检查

- `GET /api/health` → `{"status":"ok"}`
- `docker compose ps` 查看各服务 health 状态。

## 十一、常见问题

- **客户登录后显示「未授权」**:管理员需在客户管理授予时间授权(选交易所+起止时间)。
- **信号不下单**:检查①客户是否关注该 KOL ②授权是否有效 ③风控静默时段 ④交易所账号是否导入且为测试网/实盘匹配 ⑤信号汇总页看处理状态(去重/纠错/拒绝原因)。
- **下单报交易所错误**:信号汇总/告警日志看 `error`;常见为品种不存在、精度不足、保证金不够。
