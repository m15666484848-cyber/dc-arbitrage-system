#!/bin/bash
# KOL signal audit: bash /opt/dcquant/audit.sh [YYYY-MM-DD]
D=${1:-$(date -u +%F)}
OUT=/opt/dcquant/backend/reports/audit_${D//-/}.md
mkdir -p /opt/dcquant/backend/reports
docker exec -i -w /app dcquant-backend python - $D < /opt/dcquant/backend/scripts/audit_kol.py > $OUT
echo report: $OUT