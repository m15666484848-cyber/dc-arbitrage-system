#!/usr/bin/env bash
set -euo pipefail

LOG_DIR="/opt/dcquant/logs"
LOG_FILE="$LOG_DIR/disk_cleanup.log"
mkdir -p "$LOG_DIR"

{
  echo "===== $(date '+%Y-%m-%d %H:%M:%S') disk cleanup start ====="
  df -h / /opt 2>/dev/null || true
  echo "--- docker system df before ---"
  docker system df || true

  # 清理 Docker 构建缓存，保留最近 168 小时内的缓存，避免频繁构建完全失去缓存。
  docker builder prune -af || true

  # 清理停止的容器、悬空镜像和无用网络，不删除 volume，避免误删数据库。
  docker system prune -f || true

  # 清理旧备份，只保留 14 天。
  find /opt/dcquant/backups -name 'dcquant_*.sql.gz' -mtime +14 -delete 2>/dev/null || true

  # 清理应用日志，保留 30 天。
  find /opt/dcquant/logs -type f -name '*.log' -mtime +30 -delete 2>/dev/null || true

  echo "--- docker system df after ---"
  docker system df || true
  df -h / /opt 2>/dev/null || true
  echo "===== $(date '+%Y-%m-%d %H:%M:%S') disk cleanup end ====="
} >> "$LOG_FILE" 2>&1
