#!/usr/bin/env bash
set -euo pipefail

PROJECT_DIR="/opt/dcquant"
LOG_DIR="$PROJECT_DIR/logs"
LOG_FILE="$LOG_DIR/memory_guard.log"
CONTAINER_NAME="dcquant-backend"
SERVICE_NAME="backend"
THRESHOLD_MB="${BACKEND_MEMORY_RESTART_THRESHOLD_MB:-650}"
COOLDOWN_MINUTES="${BACKEND_MEMORY_RESTART_COOLDOWN_MINUTES:-15}"

mkdir -p "$LOG_DIR"

log() {
  echo "[$(date '+%Y-%m-%d %H:%M:%S')] $*" >> "$LOG_FILE"
}

to_mb() {
  local value="$1"
  python3 - "$value" <<'PY'
import re
import sys

raw = sys.argv[1].strip()
m = re.match(r"^([0-9]+(?:\.[0-9]+)?)([A-Za-z]+)$", raw)
if not m:
    print(0)
    raise SystemExit

num = float(m.group(1))
unit = m.group(2).lower()
factor = {
    "b": 1 / 1024 / 1024,
    "kb": 1 / 1024,
    "kib": 1 / 1024,
    "mb": 1,
    "mib": 1,
    "gb": 1024,
    "gib": 1024,
}.get(unit, 0)
print(int(num * factor))
PY
}

if ! docker ps --format '{{.Names}}' | grep -qx "$CONTAINER_NAME"; then
  log "skip: container_not_running name=$CONTAINER_NAME"
  exit 0
fi

usage_text="$(docker stats --no-stream --format '{{.MemUsage}}' "$CONTAINER_NAME" 2>/dev/null | awk '{print $1}')"
if [ -z "$usage_text" ]; then
  log "skip: empty_memory_usage name=$CONTAINER_NAME"
  exit 0
fi

usage_mb="$(to_mb "$usage_text")"
log "check: container=$CONTAINER_NAME usage=${usage_mb}MB threshold=${THRESHOLD_MB}MB"

if [ "$usage_mb" -lt "$THRESHOLD_MB" ]; then
  exit 0
fi

stamp_file="/tmp/dcquant_memory_guard_last_restart"
now="$(date +%s)"
if [ -f "$stamp_file" ]; then
  last="$(cat "$stamp_file" 2>/dev/null || echo 0)"
  cooldown_seconds=$((COOLDOWN_MINUTES * 60))
  if [ $((now - last)) -lt "$cooldown_seconds" ]; then
    log "skip_restart: cooldown_active usage=${usage_mb}MB cooldown=${COOLDOWN_MINUTES}min"
    exit 0
  fi
fi

log "restart: usage=${usage_mb}MB >= threshold=${THRESHOLD_MB}MB service=$SERVICE_NAME"
cd "$PROJECT_DIR"
docker compose restart "$SERVICE_NAME" >> "$LOG_FILE" 2>&1
date +%s > "$stamp_file"
sleep 8
docker compose ps "$SERVICE_NAME" >> "$LOG_FILE" 2>&1 || true
log "restart_done: service=$SERVICE_NAME"
