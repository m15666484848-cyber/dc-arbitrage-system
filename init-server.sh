#!/bin/bash
# ============================================================
# 服务器初始化脚本 - 安装 Docker / Docker Compose / 基础依赖
# 支持: Ubuntu 20.04/22.04, Debian 11/12, CentOS 7/8, RHEL 8/9
# 用法: chmod +x init-server.sh && sudo ./init-server.sh
# ============================================================

set -e

# 颜色定义
RED='\033[0;31m'
GREEN='\033[0;32m'
YELLOW='\033[1;33m'
BLUE='\033[0;34m'
CYAN='\033[0;36m'
NC='\033[0m'

echo -e "${BLUE}============================================================${NC}"
echo -e "${BLUE}  DC 量化系统 - 服务器初始化脚本${NC}"
echo -e "${BLUE}============================================================${NC}"
echo ""

# ============ 检查 root 权限 ============
if [ "$EUID" -ne 0 ]; then
    echo -e "${RED}❌ 请使用 sudo 或 root 用户运行此脚本${NC}"
    echo "命令: sudo $0"
    exit 1
fi

# ============ 检测操作系统 ============
echo -e "${BLUE}[1/5] 检测操作系统...${NC}"

if [ -f /etc/os-release ]; then
    . /etc/os-release
    OS=$ID
    OS_VERSION=$VERSION_ID
    echo -e "  检测到: ${CYAN}${PRETTY_NAME}${NC}"
else
    echo -e "${RED}❌ 无法检测操作系统${NC}"
    exit 1
fi

# ============ 系统更新与基础依赖 ============
echo ""
echo -e "${BLUE}[2/5] 更新系统并安装基础依赖...${NC}"

case $OS in
    ubuntu|debian)
        echo "  使用 apt 管理器..."
        export DEBIAN_FRONTEND=noninteractive
        apt-get update -y
        apt-get upgrade -y
        apt-get install -y \
            curl \
            wget \
            git \
            vim \
            nano \
            htop \
            net-tools \
            ca-certificates \
            gnupg \
            lsb-release \
            software-properties-common \
            openssl \
            python3 \
            python3-pip \
            unzip \
            tesseract-ocr \
            tesseract-ocr-eng \
            tesseract-ocr-chi-sim
        ;;
    centos|rhel|rocky|almalinux)
        echo "  使用 yum/dnf 管理器..."
        if command -v dnf >/dev/null 2>&1; then
            PACKAGE_MANAGER="dnf"
        else
            PACKAGE_MANAGER="yum"
        fi
        $PACKAGE_MANAGER update -y
        $PACKAGE_MANAGER install -y \
            curl \
            wget \
            git \
            vim \
            nano \
            htop \
            net-tools \
            ca-certificates \
            gnupg \
            openssl \
            python3 \
            python3-pip \
            unzip
        # Tesseract OCR (CentOS 需要 EPEL)
        $PACKAGE_MANAGER install -y epel-release || true
        $PACKAGE_MANAGER install -y tesseract tesseract-langpack-chi_sim || true
        ;;
    *)
        echo -e "${YELLOW}⚠️  未知操作系统: $OS，跳过基础依赖安装${NC}"
        ;;
esac

echo -e "  ${GREEN}✅ 基础依赖安装完成${NC}"

# ============ 安装 Docker ============
echo ""
echo -e "${BLUE}[3/5] 安装 Docker...${NC}"

if command -v docker >/dev/null 2>&1; then
    DOCKER_VERSION=$(docker --version | awk '{print $3}' | sed 's/,//')
    echo -e "  ${GREEN}✅ Docker 已安装: v${DOCKER_VERSION}${NC}"
else
    echo "  开始安装 Docker..."
    case $OS in
        ubuntu|debian)
            # 官方 Docker 仓库
            curl -fsSL https://download.docker.com/linux/${OS}/gpg | gpg --dearmor -o /usr/share/keyrings/docker-archive-keyring.gpg 2>/dev/null || \
            curl -fsSL https://mirrors.aliyun.com/docker-ce/linux/${OS}/gpg | gpg --dearmor -o /usr/share/keyrings/docker-archive-keyring.gpg

            echo "deb [arch=$(dpkg --print-architecture) signed-by=/usr/share/keyrings/docker-archive-keyring.gpg] https://download.docker.com/linux/${OS} $(lsb_release -cs) stable" \
                | tee /etc/apt/sources.list.d/docker.list > /dev/null

            apt-get update -y
            apt-get install -y docker-ce docker-ce-cli containerd.io docker-buildx-plugin docker-compose-plugin
            ;;
        centos|rhel|rocky|almalinux)
            # CentOS/RHEL
            yum-config-manager --add-repo https://download.docker.com/linux/centos/docker-ce.repo || \
            yum-config-manager --add-repo https://mirrors.aliyun.com/docker-ce/linux/centos/docker-ce.repo

            $PACKAGE_MANAGER install -y docker-ce docker-ce-cli containerd.io docker-buildx-plugin docker-compose-plugin
            ;;
    esac

    # 启动 Docker
    systemctl enable docker
    systemctl start docker

    # 添加当前用户到 docker 组（避免每次 sudo）
    if [ -n "$SUDO_USER" ]; then
        usermod -aG docker "$SUDO_USER"
        echo -e "  ${YELLOW}ℹ️  已将用户 $SUDO_USER 添加到 docker 组，重新登录后生效${NC}"
    fi

    DOCKER_VERSION=$(docker --version | awk '{print $3}' | sed 's/,//')
    echo -e "  ${GREEN}✅ Docker 安装完成: v${DOCKER_VERSION}${NC}"
fi

# ============ 安装 Docker Compose Plugin ============
echo ""
echo -e "${BLUE}[4/5] 安装 Docker Compose Plugin...${NC}"

if docker compose version >/dev/null 2>&1; then
    COMPOSE_VERSION=$(docker compose version | awk '{print $4}' | sed 's/,//')
    echo -e "  ${GREEN}✅ Docker Compose Plugin 已安装: v${COMPOSE_VERSION}${NC}"
else
    echo "  开始安装 Docker Compose Plugin..."
    # 使用 Docker 官方安装
    case $OS in
        ubuntu|debian)
            apt-get install -y docker-compose-plugin
            ;;
        centos|rhel|rocky|almalinux)
            $PACKAGE_MANAGER install -y docker-compose-plugin
            ;;
    esac

    # 如果包管理器安装失败，尝试手动安装
    if ! docker compose version >/dev/null 2>&1; then
        echo "  尝试手动下载安装..."
        COMPOSE_VERSION="v2.27.0"
        ARCH=$(uname -m)
        case $ARCH in
            x86_64) ARCH="x86_64" ;;
            aarch64) ARCH="aarch64" ;;
            *) ARCH="x86_64" ;;
        esac
        mkdir -p /usr/local/lib/docker/cli-plugins
        curl -SL "https://github.com/docker/compose/releases/download/${COMPOSE_VERSION}/docker-compose-linux-${ARCH}" \
            -o /usr/local/lib/docker/cli-plugins/docker-compose
        chmod +x /usr/local/lib/docker/cli-plugins/docker-compose
    fi

    COMPOSE_VERSION=$(docker compose version | awk '{print $4}' | sed 's/,//')
    echo -e "  ${GREEN}✅ Docker Compose Plugin 安装完成: v${COMPOSE_VERSION}${NC}"
fi

# ============ 配置 Docker (可选优化) ============
echo ""
echo -e "${BLUE}[5/5] 优化 Docker 配置...${NC}"

# 创建 Docker 配置目录
mkdir -p /etc/docker

# 配置 Docker 镜像加速（如果是国内服务器）
if [ ! -f /etc/docker/daemon.json ]; then
    cat > /etc/docker/daemon.json << EOF
{
  "log-driver": "json-file",
  "log-opts": {
    "max-size": "100m",
    "max-file": "3"
  },
  "storage-driver": "overlay2"
}
EOF
    echo -e "  ${GREEN}✅ Docker 日志限制配置已设置${NC}"

    # 重启 Docker 让配置生效
    systemctl restart docker
else
    echo -e "  ${YELLOW}ℹ️  /etc/docker/daemon.json 已存在，跳过配置${NC}"
fi

# 配置防火墙 (简单开放端口)
echo ""
echo "  开放端口: 5173 (前端), 8000 (后端)"
if command -v ufw >/dev/null 2>&1; then
    ufw allow 5173/tcp comment "DC Quant Frontend" || true
    ufw allow 8000/tcp comment "DC Quant Backend" || true
    echo -e "  ${GREEN}✅ UFW 防火墙规则已添加${NC}"
fi
if command -v firewall-cmd >/dev/null 2>&1; then
    firewall-cmd --permanent --add-port=5173/tcp --zone=public || true
    firewall-cmd --permanent --add-port=8000/tcp --zone=public || true
    firewall-cmd --reload || true
    echo -e "  ${GREEN}✅ firewalld 防火墙规则已添加${NC}"
fi

# ============ 完成 ============
echo ""
echo -e "${BLUE}============================================================${NC}"
echo -e "${GREEN}  🎉 服务器初始化完成！${NC}"
echo -e "${BLUE}============================================================${NC}"
echo ""

# 版本信息
echo -e "  📦 已安装版本:"
echo -e "     Docker:        $(docker --version 2>/dev/null | awk '{print $3}' | sed 's/,//')"
echo -e "     Docker Compose: $(docker compose version 2>/dev/null | awk '{print $4}' | sed 's/,//')"
echo -e "     Python:        $(python3 --version 2>/dev/null | awk '{print $2}')"
echo -e "     Tesseract OCR: $(tesseract --version 2>/dev/null | head -1 | awk '{print $2}')"
echo ""

# 提示信息
SERVER_IP=$(hostname -I | awk '{print $1}')
echo -e "  🌐 服务器 IP: ${CYAN}${SERVER_IP}${NC}"
echo ""
echo -e "  🚀 下一步操作:"
echo -e "     1. 重新登录 SSH (让 docker 组权限生效): exit && ssh user@${SERVER_IP}"
echo -e "     2. 上传项目文件到服务器"
echo -e "     3. 运行部署脚本: sudo bash deploy.sh"
echo ""
echo -e "  ⚠️  重要:"
echo -e "     - 生产环境建议配置 HTTPS (Let's Encrypt / Nginx)"
echo -e "     - 生产环境建议限制防火墙，只开放必要端口"
echo -e "     - 生产环境建议设置自动更新和安全补丁"

# 提示用户重新登录
if [ -n "$SUDO_USER" ]; then
    echo ""
    echo -e "  ${YELLOW}💡 提示: 请重新登录 SSH，使 docker 组权限生效${NC}"
fi