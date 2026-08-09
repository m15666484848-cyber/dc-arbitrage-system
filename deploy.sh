#!/bin/bash
# ============================================================
# DC 量化系统 - 一键部署脚本
# 用法: chmod +x deploy.sh && sudo ./deploy.sh [--external-db]
# ============================================================

set -e

# 颜色定义
RED='\033[0;31m'
GREEN='\033[0;32m'
YELLOW='\033[1;33m'
BLUE='\033[0;34m'
NC='\033[0m' # No Color

# 获取脚本目录
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
cd "$SCRIPT_DIR"

echo -e "${BLUE}============================================================${NC}"
echo -e "${BLUE}  DC 量化系统 - 一键部署脚本${NC}"
echo -e "${BLUE}============================================================${NC}"
echo ""

# ============ 检查参数 ============
EXTERNAL_DB=false
for arg in "$@"; do
    case $arg in
        --external-db)
            EXTERNAL_DB=true
            ;;
        -h|--help)
            echo "用法: $0 [选项]"
            echo ""
            echo "选项:"
            echo "  --external-db    使用外部数据库 (PostgreSQL/Redis 不内置)"
            echo "  -h, --help       显示帮助信息"
            exit 0
            ;;
    esac
done

# ============ 检查 root 权限 ============
if [ "$EUID" -ne 0 ]; then
    echo -e "${YELLOW}⚠️  请使用 sudo 或 root 用户运行此脚本${NC}"
    echo "命令: sudo $0"
    exit 1
fi

# ============ 检测 Docker Compose 命令 ============
echo -e "${BLUE}[1/6] 检测 Docker 环境...${NC}"

# 优先使用 docker compose (v2)，回退到 docker-compose (v1)
if docker compose version >/dev/null 2>&1; then
    DOCKER_COMPOSE="docker compose"
    echo -e "  ${GREEN}✅ 检测到: docker compose v2${NC}"
elif command -v docker-compose >/dev/null 2>&1; then
    DOCKER_COMPOSE="docker-compose"
    echo -e "  ${GREEN}✅ 检测到: docker-compose v1${NC}"
else
    echo -e "  ${RED}❌ 未检测到 Docker Compose${NC}"
    echo -e "  请先运行: bash init-server.sh"
    exit 1
fi

# 检查 Docker 权限
if ! docker ps >/dev/null 2>&1; then
    echo -e "  ${RED}❌ 当前用户无 Docker 控制权限${NC}"
    echo "  请执行: usermod -aG docker $USER"
    echo "  然后重新登录"
    exit 1
fi

# ============ 配置环境变量 ============
echo ""
echo -e "${BLUE}[2/6] 配置环境变量...${NC}"

ENV_FILE="$SCRIPT_DIR/.env"
BACKEND_ENV_FILE="$SCRIPT_DIR/backend/.env"

# 创建根目录 .env
if [ ! -f "$ENV_FILE" ]; then
    echo "  创建根目录 .env 文件..."
    POSTGRES_PASSWORD=$(openssl rand -hex 16)
    cat > "$ENV_FILE" << EOF
# ============================================================
# DC Quant 部署配置
# ============================================================

# 端口配置
FRONTEND_PORT=5173
BACKEND_PORT=8000

# PostgreSQL 配置 (仅内置数据库时使用)
POSTGRES_USER=dcquant
POSTGRES_PASSWORD=$POSTGRES_PASSWORD
POSTGRES_DB=dcquant
POSTGRES_PORT=5432

# Redis 配置 (仅内置 Redis 时使用)
REDIS_PORT=6379
EOF
    echo -e "  ${GREEN}✅ 创建 .env 文件${NC}"
else
    echo -e "  ${YELLOW}ℹ️  .env 文件已存在，跳过创建${NC}"
fi

# 创建后端 .env
if [ ! -f "$BACKEND_ENV_FILE" ]; then
    echo "  创建 backend/.env 文件..."

    # 从根 .env 读取数据库配置
    source "$ENV_FILE"

    # 生成 SECRET_KEY
    SECRET_KEY=$(openssl rand -hex 32)
    ENCRYPT_KEY=$(python3 -c "from cryptography.fernet import Fernet; print(Fernet.generate_key().decode())" 2>/dev/null || echo "generate-manually")

    if [ "$EXTERNAL_DB" = true ]; then
        DB_HOST="your-external-db-host"
        DB_PORT="5432"
        DB_USER="your-db-user"
        DB_PASSWORD="your-db-password"
        DB_NAME="your-db-name"
        REDIS_URL="redis://your-external-redis-host:6379/0"
    else
        DB_HOST="postgres"
        DB_PORT="5432"
        DB_USER="${POSTGRES_USER:-dcquant}"
        DB_PASSWORD="${POSTGRES_PASSWORD:-dcquant}"
        DB_NAME="${POSTGRES_DB:-dcquant}"
        REDIS_URL="redis://redis:6379/0"
    fi

    cat > "$BACKEND_ENV_FILE" << EOF
# ============================================================
# DC Quant 后端配置
# ============================================================

# 环境模式: dev / prod
ENV=prod

# 数据库连接 (重要: 根据实际情况修改)
DATABASE_URL=postgresql+asyncpg://${DB_USER}:${DB_PASSWORD}@${DB_HOST}:${DB_PORT}/${DB_NAME}

# Redis 连接
REDIS_URL=${REDIS_URL}

# 安全配置 (自动生成，请勿泄露！)
SECRET_KEY=${SECRET_KEY}
ENCRYPT_KEY=${ENCRYPT_KEY}

# JWT 配置
ACCESS_TOKEN_EXPIRE_MINUTES=43200

# ============================================================
# Discord 配置 (重要!)
# ============================================================
# 请填写您的 Discord 用户 TOKEN (用于监听 KOL 频道)
DISCORD_TOKEN=

# ============================================================
# 交易所配置 (支持 OKX, Binance, Bybit)
# ============================================================
# 启用的交易所 (逗号分隔)
ENABLED_EXCHANGES=okx,binance,bybit

# OKX 测试网 (默认启用测试)
OKX_SANDBOX=true
OKX_API_KEY=
OKX_API_SECRET=
OKX_PASSPHRASE=

# OKX 实盘 (上线生产时使用)
# OKX_SANDBOX=false
# OKX_API_KEY=
# OKX_API_SECRET=
# OKX_PASSPHRASE=

# Binance
BINANCE_API_KEY=
BINANCE_API_SECRET=
BINANCE_SANDBOX=true

# Bybit
BYBIT_API_KEY=
BYBIT_API_SECRET=
BYBIT_SANDBOX=true

# ============================================================
# LLM 智能解析配置 (可选,运行时也可在管理后台修改)
# 双 LLM 架构: 文本走 DeepSeek V3, 图片走 GLM-4V
# ============================================================
# 全局开关 (关闭后两个模型都不调用)
LLM_ENABLED=false

# ---- 文本 LLM (DeepSeek V3,解析信号文本) ----
LLM_PROVIDER=deepseek
LLM_API_KEY=
LLM_MODEL=deepseek-chat
LLM_API_BASE=https://api.deepseek.com/v1
LLM_TEMPERATURE=0.1
LLM_MAX_TOKENS=2000
LLM_TIMEOUT=30

# ---- 图片 LLM (GLM-4V,解析图片信号,仅对勾选的 KOL 生效) ----
# 留空则复用文本 LLM 的 Key(若同提供商)
VISION_LLM_ENABLED=false
VISION_LLM_PROVIDER=zhipu
VISION_LLM_API_KEY=
VISION_LLM_MODEL=glm-4v
VISION_LLM_API_BASE=https://open.bigmodel.cn/api/paas/v4

# ============================================================
# 飞书告警 Webhook (可选)
# ============================================================
FEISHU_WEBHOOK_URL=

# ============================================================
# 风控配置 (重要!)
# ============================================================
# 每个客户最大同时持仓数
MAX_OPEN_POSITIONS_PER_CUSTOMER=10

# 单订单最大金额 (USDT)
MAX_ORDER_AMOUNT=1000

# 每日最大亏损比例 (0.1 = 10%)
DAILY_MAX_LOSS_RATIO=0.1
EOF
    echo -e "  ${GREEN}✅ 创建 backend/.env 文件${NC}"
    echo -e "  ${YELLOW}⚠️  请编辑 backend/.env 文件填写:${NC}"
    echo "     1. DISCORD_TOKEN"
    echo "     2. OKX / Binance / Bybit API Key"
    if [ "$EXTERNAL_DB" = true ]; then
        echo "     3. 外部数据库连接信息"
    fi
else
    echo -e "  ${YELLOW}ℹ️  backend/.env 文件已存在，跳过创建${NC}"
fi

# ============ 构建 Docker 镜像 ============
echo ""
echo -e "${BLUE}[3/6] 构建 Docker 镜像...${NC}"
echo "  这可能需要几分钟时间..."

# 选择 compose 文件
if [ "$EXTERNAL_DB" = true ]; then
    COMPOSE_FILE="-f docker-compose.external-db.yml"
    echo "  使用: docker-compose.external-db.yml (外部数据库模式)"
else
    COMPOSE_FILE=""
    echo "  使用: docker-compose.yml (内置数据库模式)"
fi

# 构建镜像
$DOCKER_COMPOSE $COMPOSE_FILE build --no-cache 2>&1 | tail -5

echo -e "  ${GREEN}✅ 镜像构建完成${NC}"

# ============ 启动服务 ============
echo ""
echo -e "${BLUE}[4/6] 启动服务...${NC}"

$DOCKER_COMPOSE $COMPOSE_FILE up -d

echo -e "  ${GREEN}✅ 服务启动中${NC}"

# ============ 等待服务就绪 ============
echo ""
echo -e "${BLUE}[5/6] 等待服务就绪...${NC}"
echo "  等待 PostgreSQL 和 Redis 启动..."

MAX_WAIT=60
WAITED=0
while [ $WAITED -lt $MAX_WAIT ]; do
    sleep 3
    WAITED=$((WAITED + 3))

    # 检查后端健康
    if curl -sf http://localhost:8000/api/health >/dev/null 2>&1; then
        echo -e "  ${GREEN}✅ 后端服务就绪 (${WAITED}s)${NC}"
        break
    fi

    # 如果是外部数据库模式，跳过 DB 检查
    if [ "$EXTERNAL_DB" = false ]; then
        # 检查 PostgreSQL
        if ! docker ps | grep dcquant-postgres >/dev/null; then
            echo -e "  ${RED}❌ PostgreSQL 容器未运行${NC}"
            docker logs dcquant-postgres 2>&1 | tail -10
            exit 1
        fi
    fi

    echo "  等待中... (${WAITED}/${MAX_WAIT}s)"
done

# ============ 数据库迁移和初始化 ============
echo ""
echo -e "${BLUE}[6/6] 数据库迁移和初始化...${NC}"

# 运行数据库迁移
echo "  运行 Alembic 迁移..."
$DOCKER_COMPOSE $COMPOSE_FILE exec -T backend alembic upgrade head 2>&1 | tail -5

# 询问是否创建管理员账号
echo ""
read -p "  是否创建默认管理员账号? (Y/n): " CREATE_ADMIN
CREATE_ADMIN=${CREATE_ADMIN:-Y}
if [[ "$CREATE_ADMIN" =~ ^[Yy]$ ]]; then
    read -p "  管理员用户名 [admin]: " ADMIN_USER
    ADMIN_USER=${ADMIN_USER:-admin}
    read -s -p "  管理员密码 [admin123]: " ADMIN_PASS
    echo ""
    ADMIN_PASS=${ADMIN_PASS:-admin123}

    echo "  创建管理员账号: ${ADMIN_USER}"
    $DOCKER_COMPOSE $COMPOSE_FILE exec -T backend python init_admin.py "$ADMIN_USER" "$ADMIN_PASS" 2>&1 || true
fi

# ============ 完成 ============
echo ""
echo -e "${BLUE}============================================================${NC}"
echo -e "${GREEN}  🎉 部署完成！${NC}"
echo -e "${BLUE}============================================================${NC}"
echo ""

# 获取服务器 IP
SERVER_IP=$(hostname -I | awk '{print $1}')

echo -e "  🌐 访问地址:"
echo -e "     前端: http://${SERVER_IP}:${FRONTEND_PORT:-5173}"
echo -e "     后端: http://${SERVER_IP}:${BACKEND_PORT:-8000}/api/docs"
echo ""
echo -e "  🔑 登录信息:"
if [ "$ADMIN_USER" ]; then
    echo -e "     用户名: ${ADMIN_USER}"
    echo -e "     密码: ${ADMIN_PASS}"
else
    echo -e "     用户名: admin (如果已创建)"
    echo -e "     密码: admin123 (如果已创建)"
fi
echo ""
echo -e "  📋 常用命令:"
echo -e "     查看服务状态: $DOCKER_COMPOSE $COMPOSE_FILE ps"
echo -e "     查看日志: $DOCKER_COMPOSE $COMPOSE_FILE logs -f backend"
echo -e "     重启服务: $DOCKER_COMPOSE $COMPOSE_FILE restart"
echo -e "     停止服务: $DOCKER_COMPOSE $COMPOSE_FILE down"
echo ""
echo -e "  ⚠️  重要提示:"
echo -e "     1. 请立即修改默认密码"
echo -e "     2. 编辑 backend/.env 配置交易所 API Key"
echo -e "     3. 生产环境请配置 HTTPS 和防火墙"
echo -e "     4. 如需使用外部数据库，重新运行: sudo $0 --external-db"