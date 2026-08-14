#!/bin/bash
LOG="/opt/dcquant/logs/dcquant_monitor.log"
TIMESTAMP=$(date '+%Y-%m-%d %H:%M:%S')

UNHEALTHY=$(docker ps --filter "health=unhealthy" --filter "name=dcquant" -q 2>/dev/null | wc -l)
DOWN=$(docker ps -a --filter "status=exited" --filter "name=dcquant" -q 2>/dev/null | wc -l)
BACKEND_OK=$(curl -sf http://127.0.0.1:8000/api/health 2>/dev/null | grep -c "ok")
DB_OK=$(docker exec dcquant-postgres pg_isready -U dcquant 2>/dev/null | grep -c "accepting")
REDIS_OK=$(docker exec dcquant-redis redis-cli ping 2>/dev/null | grep -c "PONG")

STATUS="OK"
ISSUES=""
if [ "$UNHEALTHY" -gt 0 ]; then STATUS="CRITICAL"; ISSUES="$ISSUES container_unhealthy($UNHEALTHY)"; fi
if [ "$DOWN" -gt 0 ]; then STATUS="CRITICAL"; ISSUES="$ISSUES container_down($DOWN)"; fi
if [ "$BACKEND_OK" -ne 1 ]; then STATUS="CRITICAL"; ISSUES="$ISSUES backend_down"; fi
if [ "$DB_OK" -ne 1 ]; then STATUS="CRITICAL"; ISSUES="$ISSUES db_down"; fi
if [ "$REDIS_OK" -ne 1 ]; then STATUS="WARNING"; ISSUES="$ISSUES redis_down"; fi

echo "[$TIMESTAMP] status=$STATUS backend=$BACKEND_OK db=$DB_OK redis=$REDIS_OK issues=$ISSUES" >> $LOG

if [ "$STATUS" = "CRITICAL" ]; then
    docker exec dcquant-backend python /app/scripts/send_alert.py "DCQuant 服务告警" "Status: $STATUS Issues:$ISSUES" >/dev/null 2>&1
fi
