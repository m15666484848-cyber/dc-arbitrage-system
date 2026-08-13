#!/bin/bash
# DCQuant 数据库自动备份脚本
# S10新增: 每日凌晨3点自动备份数据库,保留最近30天

set -euo pipefail

BACKUP_DIR="/opt/dcquant/dcquant_backups"
DB_USER="${POSTGRES_USER:-dcquant}"
DB_NAME="${POSTGRES_DB:-dcquant}"
DB_CONTAINER="dcquant-postgres"
RETENTION_DAYS=30
TIMESTAMP=$(date +%Y%m%d_%H%M%S)
BACKUP_FILE="${BACKUP_DIR}/dcquant_${TIMESTAMP}.sql.gz"

# 确保备份目录存在
mkdir -p "${BACKUP_DIR}"

echo "[$(date)] 开始数据库备份..."

# 执行备份(通过docker exec)
if docker exec "${DB_CONTAINER}" pg_dump -U "${DB_USER}" "${DB_NAME}" 2>/dev/null | gzip > "${BACKUP_FILE}"; then
    FILESIZE=$(du -h "${BACKUP_FILE}" | cut -f1)
    echo "[$(date)] 备份成功: ${BACKUP_FILE} (${FILESIZE})"
else
    echo "[$(date)] 备份失败!"
    rm -f "${BACKUP_FILE}"
    exit 1
fi

# 清理过期备份
echo "[$(date)] 清理 ${RETENTION_DAYS} 天前的旧备份..."
DELETED=$(find "${BACKUP_DIR}" -name "dcquant_*.sql.gz" -mtime +${RETENTION_DAYS} -print -delete | wc -l)
if [ "${DELETED}" -gt 0 ]; then
    echo "[$(date)] 已删除 ${DELETED} 个过期备份文件"
fi

# 列出当前备份
BACKUP_COUNT=$(find "${BACKUP_DIR}" -name "dcquant_*.sql.gz" | wc -l)
TOTAL_SIZE=$(du -sh "${BACKUP_DIR}" | cut -f1)
echo "[$(date)] 当前备份: ${BACKUP_COUNT} 个文件, 总大小: ${TOTAL_SIZE}"
echo "[$(date)] 备份完成"
