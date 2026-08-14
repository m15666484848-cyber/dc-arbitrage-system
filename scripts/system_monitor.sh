#!/usr/bin/env bash
# DCQuant 系统资源监控 — 磁盘/CPU/内存/容器
# 每5分钟运行,异常时飞书告警,恢复时通知
# 使用 send_alert.py 发送告警(避免 curl 9499 错误)
set -euo pipefail

LOG_FILE="/opt/dcquant/logs/system_monitor.log"
STATE_DIR="/tmp/dcquant_alert_state"
TIMESTAMP=$(date '+%Y-%m-%d %H:%M:%S')

# 阈值
DISK_THRESHOLD=80        # 磁盘使用率 %
MEM_THRESHOLD=85         # 内存使用率 %
LOAD_THRESHOLD=2.0       # CPU load average (1min)
BACKEND_MEM_THRESHOLD=90 # 后端容器内存使用率占限制 %

mkdir -p "$(dirname "$LOG_FILE")" "$STATE_DIR"

log() { echo "[$TIMESTAMP] $*" >> "$LOG_FILE"; }

send_alert() {
    local title="$1"
    local message="$2"
    log "ALERT: $title - $message"
    docker exec dcquant-backend python /app/scripts/send_alert.py "$title" "$message" >/dev/null 2>&1 || log "send_alert failed"
}

send_recovery() {
    local title="$1"
    local message="$2"
    log "RECOVERY: $title - $message"
    docker exec dcquant-backend python /app/scripts/send_alert.py "$title" "$message" >/dev/null 2>&1 || log "send_recovery failed"
}

# 告警状态管理: 只在状态变化时发通知
check_and_alert() {
    local alert_key="$1"
    local title="$2"
    local message="$3"
    local state_file="$STATE_DIR/${alert_key}"

    if [ -f "$state_file" ]; then
        log "still_alert: $alert_key (suppressed)"
    else
        send_alert "$title" "$message"
        echo "$TIMESTAMP" > "$state_file"
    fi
}

check_and_recover() {
    local alert_key="$1"
    local title="$2"
    local message="$3"
    local state_file="$STATE_DIR/${alert_key}"

    if [ -f "$state_file" ]; then
        send_recovery "$title" "$message"
        rm -f "$state_file"
    fi
}

# === 1. 磁盘使用率 ===
check_disk() {
    local usage
    usage=$(df / | awk 'NR==2 {gsub(/%/,""); print $5}')
    local free_gb
    free_gb=$(df -h / | awk 'NR==2 {print $4}')

    if [ "$usage" -ge "$DISK_THRESHOLD" ]; then
        check_and_alert "disk" "磁盘空间告警" "磁盘使用率 ${usage}% (阈值 ${DISK_THRESHOLD}%) 剩余空间: ${free_gb} 路径: /"
    else
        check_and_recover "disk" "磁盘空间恢复" "磁盘使用率已降至 ${usage}% 剩余空间: ${free_gb}"
    fi
    log "disk: usage=${usage}% free=${free_gb}"
}

# === 2. 内存使用率 ===
check_memory() {
    local total used avail usage_pct
    total=$(free -m | awk '/^Mem:/ {print $2}')
    used=$(free -m | awk '/^Mem:/ {print $3}')
    avail=$(free -m | awk '/^Mem:/ {print $7}')
    usage_pct=$(( (total - avail) * 100 / total ))

    if [ "$usage_pct" -ge "$MEM_THRESHOLD" ]; then
        check_and_alert "memory" "内存使用告警" "内存使用率 ${usage_pct}% (阈值 ${MEM_THRESHOLD}%) 总计: ${total}MB 可用: ${avail}MB"
    else
        check_and_recover "memory" "内存使用恢复" "内存使用率已降至 ${usage_pct}% 可用: ${avail}MB"
    fi
    log "memory: usage=${usage_pct}% avail=${avail}MB"
}

# === 3. CPU 负载 ===
check_cpu() {
    local load1
    load1=$(awk '{print $1}' /proc/loadavg)
    if awk "BEGIN {exit !($load1 >= $LOAD_THRESHOLD)}"; then
        check_and_alert "cpu" "CPU 负载告警" "CPU 负载 ${load1} (阈值 ${LOAD_THRESHOLD}) $(uptime)"
    else
        check_and_recover "cpu" "CPU 负载恢复" "CPU 负载已降至 ${load1}"
    fi
    log "cpu: load1=${load1}"
}

# === 4. 后端容器内存 ===
check_backend_mem() {
    if ! docker ps --format '{{.Names}}' | grep -qx "dcquant-backend" 2>/dev/null; then
        return
    fi

    local mem_usage mem_pct
    mem_usage=$(docker stats --no-stream --format '{{.MemUsage}}' dcquant-backend 2>/dev/null)
    mem_pct=$(docker stats --no-stream --format '{{.MemPerc}}' dcquant-backend 2>/dev/null | tr -d '%')

    if [ -z "$mem_pct" ]; then
        log "backend_mem: skip (empty stats)"
        return
    fi

    if awk "BEGIN {exit !($mem_pct >= $BACKEND_MEM_THRESHOLD)}"; then
        check_and_alert "backend_mem" "后端容器内存告警" "后端容器内存 ${mem_pct}% (阈值 ${BACKEND_MEM_THRESHOLD}%) 使用: ${mem_usage}"
    else
        check_and_recover "backend_mem" "后端容器内存恢复" "后端容器内存已降至 ${mem_pct}% 使用: ${mem_usage}"
    fi
    log "backend_mem: pct=${mem_pct}% usage=${mem_usage}"
}

# === 5. 后端 API 响应时间 ===
check_api_latency() {
    local response_time
    response_time=$(curl -o /dev/null -s -w '%{time_total}' http://127.0.0.1:8000/api/health 2>/dev/null)

    if [ -z "$response_time" ]; then
        check_and_alert "api_latency" "API 无响应告警" "后端 API 健康检查无响应 请检查容器状态"
        return
    fi

    if awk "BEGIN {exit !($response_time >= 5.0)}"; then
        check_and_alert "api_latency" "API 响应延迟告警" "API 响应时间 ${response_time}s (阈值 5s)"
    else
        check_and_recover "api_latency" "API 响应恢复" "API 响应时间 ${response_time}s"
    fi
    log "api_latency: ${response_time}s"
}

# === 执行所有检查 ===
log "--- system monitor start ---"
check_disk
check_memory
check_cpu
check_backend_mem
check_api_latency
log "--- system monitor done ---"
