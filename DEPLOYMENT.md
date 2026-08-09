# DC 量化系统 - 部署指南

本文档指导您将 DC 量化系统部署到生产服务器。

---

## 📋 部署前准备

### 服务器要求

| 项目 | 最低要求 | 推荐配置 |
|------|----------|----------|
| **操作系统** | Ubuntu 20.04 / Debian 11 / CentOS 7 | Ubuntu 22.04 LTS |
| **CPU** | 2 核 | 4 核以上 |
| **内存** | 4 GB | 8 GB 以上（OCR 和 LLM 更耗内存） |
| **硬盘** | 50 GB | 100 GB SSD |
| **网络** | 10 Mbps 带宽 | 100 Mbps 以上 |
| **Docker** | 20.10+ | 最新稳定版 |

### 网络要求

- 需要访问交易所 API（OKX / Binance / Bybit）
- 需要访问 Discord Gateway（用于监听 KOL 消息）
- 如果使用 LLM，需要访问 DeepSeek / 智谱 API

### 安全建议

- 开启服务器防火墙（仅开放必要端口）
- 使用 SSH Key 登录，禁用密码登录
- 定期更新系统安全补丁
- 配置 HTTPS 证书（生产环境）
- 使用强密码和 API Key

---

## 🚀 快速部署（推荐）

### 步骤 1: 服务器初始化

```bash
# 1. 上传项目文件到服务器
# 使用 scp 或 git clone
scp -r ./ "user@your-server:/opt/dcquant"

# 2. 登录服务器
ssh user@your-server
cd /opt/dcquant

# 3. 初始化服务器（安装 Docker、Tesseract OCR 等）
chmod +x init-server.sh
sudo ./init-server.sh

# 4. 重新登录 SSH（使 docker 组权限生效）
exit
ssh user@your-server
```

### 步骤 2: 一键部署

#### 方案 A：内置数据库（推荐新服务器）

```bash
cd /opt/dcquant
chmod +x deploy.sh
sudo ./deploy.sh
```

部署脚本会自动：
- ✅ 配置环境变量（自动生成密钥）
- ✅ 构建 Docker 镜像
- ✅ 启动 PostgreSQL / Redis / 后端 / 前端
- ✅ 运行数据库迁移
- ✅ 创建默认管理员账号

#### 方案 B：使用外部数据库（已有独立数据库）

```bash
cd /opt/dcquant
chmod +x deploy.sh
sudo ./deploy.sh --external-db
```

然后编辑 `backend/.env`，填入外部数据库连接信息：
```env
DATABASE_URL=postgresql+asyncpg://user:password@host:5432/dbname
REDIS_URL=redis://host:6379/0
```

### 步骤 3: 配置必要信息

编辑 `backend/.env` 文件，填入以下信息：

```env
# ========== 必填 ==========
DISCORD_TOKEN=your_discord_token_here

# OKX 交易所（必须填一个）
OKX_SANDBOX=true
OKX_API_KEY=your_okx_api_key
OKX_API_SECRET=your_okx_api_secret
OKX_PASSPHRASE=your_okx_passphrase

# ========== 可选 ==========
# Binance
BINANCE_API_KEY=
BINANCE_API_SECRET=
BINANCE_SANDBOX=true

# Bybit
BYBIT_API_KEY=
BYBIT_API_SECRET=
BYBIT_SANDBOX=true

# LLM 智能解析（可选）
LLM_ENABLED=true
LLM_PROVIDER=deepseek  # 或 zhipu
LLM_API_KEY=your_llm_api_key

# 飞书告警（可选）
FEISHU_WEBHOOK_URL=https://open.feishu.cn/open-apis/bot/v2/hook/xxx
```

### 步骤 4: 重启服务使配置生效

```bash
# 重新构建后端（如果修改了 .env）
sudo ./deploy.sh
# 或只重启后端
docker compose restart backend
```

---

## 🌐 访问系统

部署完成后，在浏览器访问：

| 服务 | 地址 | 说明 |
|------|------|------|
| **前端** | http://your-server-ip:5173 | 主界面 |
| **后端 API** | http://your-server-ip:8000/api/docs | Swagger 文档 |
| **后端健康检查** | http://your-server-ip:8000/api/health | 状态监控 |

### 默认管理员账号

| 项 | 值 |
|----|-----|
| 用户名 | `admin` |
| 密码 | `admin123` |

⚠️ **重要：请立即修改默认密码！**

---

## 🐳 常用 Docker 命令

```bash
# 查看服务状态
docker compose ps

# 查看所有服务日志
docker compose logs -f

# 查看特定服务日志
docker compose logs -f backend
docker compose logs -f frontend
docker compose logs -f postgres

# 重启服务
docker compose restart

# 重启单个服务
docker compose restart backend

# 停止服务
docker compose down

# 停止并删除数据（危险！）
docker compose down -v

# 进入容器
docker compose exec backend bash
docker compose exec postgres psql -U dcquant -d dcquant
```

---

## 🔧 高级配置

### 配置 HTTPS（推荐生产环境）

使用 Nginx + Let's Encrypt：

```bash
# 安装 certbot
sudo apt install certbot python3-certbot-nginx -y

# 申请证书
sudo certbot --nginx -d your-domain.com

# 配置 Nginx 反向代理（将 80/443 转发到 5173）
```

### 使用外部数据库

编辑 `backend/.env`：

```env
# PostgreSQL（独立服务器）
DATABASE_URL=postgresql+asyncpg://myuser:mypassword@db-host:5432/mydb

# Redis（独立服务器）
REDIS_URL=redis://redis-host:6379/0

# 有密码的 Redis
REDIS_URL=redis://:password@redis-host:6379/0
```

然后使用外部数据库模式部署：

```bash
sudo ./deploy.sh --external-db
```

### 配置多实例部署

如果需要在多台服务器上部署：

```
负载均衡器 (Nginx/HAProxy)
    ├── Server 1: Frontend + Backend (API)
    ├── Server 2: Backend (Discord Listener)
    ├── Shared PostgreSQL Cluster
    └── Shared Redis Cluster
```

### 配置日志轮转

系统已配置 Docker 日志轮转限制（100MB × 3 文件）。

如需额外配置 `logrotate`：

```bash
cat > /etc/logrotate.d/dcquant << EOF
/var/lib/docker/containers/*/*.log {
    daily
    rotate 7
    compress
    delaycompress
    missingok
    copytruncate
}
EOF
```

---

## 📊 监控和备份

### 健康检查 API

```bash
# 检查服务状态
curl http://localhost:8000/api/health
```

响应示例：
```json
{
  "status": "healthy",
  "services": {
    "database": "ok",
    "redis": "ok",
    "discord": "connected",
    "exchanges": {
      "okx_sandbox": "ok"
    }
  }
}
```

### 数据库备份（推荐每日）

```bash
# 创建备份脚本
cat > /opt/dcquant/backup.sh << 'EOF'
#!/bin/bash
BACKUP_DIR="/opt/backups/dcquant"
DATE=$(date +%Y%m%d_%H%M%S)
mkdir -p $BACKUP_DIR

# 备份 PostgreSQL
docker compose exec -T postgres pg_dump -U dcquant dcquant | gzip > $BACKUP_DIR/postgres_$DATE.sql.gz

# 备份 Redis
docker compose exec -T redis redis-cli --rdb /tmp/dump.rdb
docker compose cp redis:/tmp/dump.rdb $BACKUP_DIR/redis_$DATE.rdb

# 删除 7 天前的备份
find $BACKUP_DIR -name "*.gz" -mtime +7 -delete
find $BACKUP_DIR -name "*.rdb" -mtime +7 -delete

echo "✅ 备份完成: $DATE"
EOF

chmod +x /opt/dcquant/backup.sh

# 添加到 crontab (每天凌晨 3 点执行)
echo "0 3 * * * /opt/dcquant/backup.sh >> /var/log/dcquant-backup.log 2>&1" | sudo crontab -
```

---

## 🆘 常见问题

### Q1: Docker 权限不足？

```
错误: permission denied while trying to connect to the Docker daemon socket
```

解决：
```bash
sudo usermod -aG docker $USER
# 重新登录 SSH
```

### Q2: 数据库连接失败？

```
错误: Connection refused: Is the server running on host postgres and accepting TCP/IP connections?
```

解决：
```bash
# 检查数据库容器状态
docker compose ps postgres

# 查看数据库日志
docker compose logs postgres

# 重启数据库
docker compose restart postgres
```

### Q3: Discord Token 无效？

```
错误: 401 Authentication failed
```

解决：
1. 检查 `DISCORD_TOKEN` 是否正确
2. 确认 Token 没有过期
3. 确认账号没有被封

### Q4: 交易所下单失败？

```
错误: Invalid API Key / Insufficient balance
```

解决：
1. 检查 API Key 权限（需要读取 + 交易权限）
2. 确认交易所账户有余额
3. 测试网和实盘不要搞混

### Q5: 内存不足？

```
错误: Out of memory
```

解决：
1. 添加 Swap 分区：
```bash
sudo fallocate -l 4G /swapfile
sudo chmod 600 /swapfile
sudo mkswap /swapfile
sudo swapon /swapfile
echo '/swapfile none swap sw 0 0' | sudo tee -a /etc/fstab
```
2. 升级服务器配置

---

## 📞 获取帮助

如果遇到问题：
1. 查看日志：`docker compose logs -f`
2. 检查健康状态：`curl http://localhost:8000/api/health`
3. 查看本文档的常见问题部分

---

## 🎉 部署完成

恭喜！DC 量化系统已成功部署。请记得：

- ✅ 修改默认管理员密码
- ✅ 配置 Discord Token
- ✅ 配置交易所 API Key
- ✅ 设置定时备份
- ✅ 配置 HTTPS（生产环境）