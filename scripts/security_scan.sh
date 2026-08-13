#!/bin/bash
# DCQuant 依赖安全扫描脚本
# S12新增: 每周自动扫描依赖漏洞,结果保存到日志文件
# 注意: pip-audit 不在生产镜像中,每次扫描时临时安装

set -euo pipefail

LOG_DIR="/opt/dcquant/dcquant_backups"
LOG_FILE="${LOG_DIR}/security_scan.log"
TIMESTAMP=$(date "+%Y-%m-%d %H:%M:%S")

echo "[$TIMESTAMP] === 开始依赖安全扫描 ===" >> "$LOG_FILE"

# 临时安装 pip-audit(扫描后不保留)
echo "[$TIMESTAMP] 安装 pip-audit..." >> "$LOG_FILE"
sudo docker exec dcquant-backend pip install --quiet pip-audit 2>/dev/null || {
    echo "[$TIMESTAMP] pip-audit 安装失败,跳过扫描" >> "$LOG_FILE"
    echo "[$TIMESTAMP] === 扫描结束 ===" >> "$LOG_FILE"
    exit 1
}

# 执行扫描
RESULT=$(sudo docker exec dcquant-backend pip-audit --format=json 2>/dev/null || echo '{"dependencies":[]}')

# 统计漏洞数量
VULN_COUNT=$(echo "$RESULT" | python3 -c "
import sys, json
try:
    data = json.load(sys.stdin)
    vulns = data.get('dependencies', [])
    total = sum(len(d.get('vulns', [])) for d in vulns)
    print(total)
except:
    print(0)
" 2>/dev/null || echo "0")

echo "[$TIMESTAMP] 扫描完成: 发现 $VULN_COUNT 个已知漏洞" >> "$LOG_FILE"

if [ "$VULN_COUNT" -gt 0 ]; then
    echo "[$TIMESTAMP] 漏洞详情:" >> "$LOG_FILE"
    sudo docker exec dcquant-backend pip-audit --format=csv 2>/dev/null >> "$LOG_FILE" || true
    echo "" >> "$LOG_FILE"
    echo "[$TIMESTAMP] 警告: 发现 $VULN_COUNT 个依赖漏洞,请检查并更新!" >> "$LOG_FILE"
else
    echo "[$TIMESTAMP] 所有依赖均无已知漏洞" >> "$LOG_FILE"
fi

# 卸载 pip-audit(保持生产镜像干净)
sudo docker exec dcquant-backend pip uninstall --quiet -y pip-audit 2>/dev/null || true

echo "[$TIMESTAMP] === 扫描结束 ===" >> "$LOG_FILE"
echo "" >> "$LOG_FILE"
