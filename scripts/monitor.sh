#!/bin/bash
LOG="/var/log/dcquant_monitor.log"
TIMESTAMP=$(date '+%Y-%m-%d %H:%M:%S')
ALERT_WEBHOOK="https://open.feishu.cn/open-apis/bot/v2/hook/eb8cfb4f-fd41-4d15-86c5-e29970500a9e"

UNHEALTHY=$(docker ps --filter "health=unhealthy" --filter "name=dcquant" -q 2>/dev/null | wc -l)
DOWN=$(docker ps -a --filter "status=exited" --filter "name=dcquant" -q 2>/dev/null | wc -l)
BACKEND_OK=$(curl -sf http://127.0.0.1:8000/health 2>/dev/null | grep -c "ok")
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
    curl -sf -X POST "$ALERT_WEBHOOK" \
        -H "Content-Type: application/json" \
        -d "{\"msg_type\":\"text\",\"content\":{\"text\":\"DCQuant ALERT [$TIMESTAMP]\nStatus: $STATUS\nIssues:$ISSUES\"}}" \
        >/dev/null 2>&1
fi
