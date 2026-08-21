#!/bin/bash
# dclh001(customer 20) 演示数据增量生成(幂等,每 5 分钟由 cron 调用):
# 每日 0 点后首次运行生成当天确定性交易计划,平仓时间到达后逐步落库,
# 净值快照按 5 分钟桶补齐。客户不存在/停用时自动静默退出。
echo "[$(date '+%F %T')] demo_data_gen run"
/usr/bin/docker exec dcquant-backend python /app/scripts/gen_demo_daily.py
