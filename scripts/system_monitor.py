#!/usr/bin/env python3
"""DCQuant 系统资源监控 — 磁盘/CPU/内存/容器/API延迟
每5分钟运行,异常时飞书告警,恢复时通知
"""
import asyncio
import os
import sys
import time
import json
import aiohttp
from pathlib import Path

sys.path.insert(0, "/app")
from app.services.notification import _send_feishu

# 告警配置
ALERT_WEBHOOKS = [
    "https://open.feishu.cn/open-apis/bot/v2/hook/eb8cfb4f-fd41-4d15-86c5-e29970500a9e",
    "https://open.feishu.cn/open-apis/bot/v2/hook/1ccf8fa3-46a9-46fa-817a-03829b8dc488",
]
STATE_FILE = "/tmp/dcquant_monitor_state.json"
LOG_FILE = "/opt/dcquant/logs/system_monitor.log"

# 阈值
DISK_THRESHOLD = 80       # 磁盘使用率 %
MEM_THRESHOLD = 85        # 内存使用率 %
LOAD_THRESHOLD = 2.0      # CPU load average (1min)
API_LATENCY_THRESHOLD = 5.0  # API 响应时间 (秒)


def log(msg: str):
    from datetime import datetime
    ts = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    line = f"[{ts}] {msg}"
    print(line)
    try:
        Path(LOG_FILE).parent.mkdir(parents=True, exist_ok=True)
        with open(LOG_FILE, "a") as f:
            f.write(line + "\n")
    except Exception:
        pass


def load_state() -> dict:
    try:
        with open(STATE_FILE) as f:
            return json.load(f)
    except Exception:
        return {}


def save_state(state: dict):
    try:
        with open(STATE_FILE, "w") as f:
            json.dump(state, f)
    except Exception:
        pass


async def send_alert(title: str, content: str, event: str = "risk"):
    for webhook in ALERT_WEBHOOKS:
        await _send_feishu(webhook, title, content, event)


def check_disk() -> tuple[float, str]:
    stat = os.statvfs("/")
    total = stat.f_blocks * stat.f_frsize
    free = stat.f_bavail * stat.f_frsize
    used_pct = (1 - free / total) * 100
    free_gb = free / (1024 ** 3)
    return used_pct, f"{free_gb:.1f}GB"


def check_memory() -> tuple[float, int, int]:
    with open("/proc/meminfo") as f:
        lines = f.readlines()
    info = {}
    for line in lines:
        parts = line.split()
        info[parts[0].rstrip(":")] = int(parts[1])
    total = info["MemTotal"]
    avail = info.get("MemAvailable", info.get("MemFree", 0))
    usage_pct = (1 - avail / total) * 100
    return usage_pct, avail // 1024, total // 1024


def check_cpu() -> float:
    with open("/proc/loadavg") as f:
        return float(f.read().split()[0])


async def check_api_latency() -> float:
    try:
        start = time.time()
        async with aiohttp.ClientSession() as session:
            async with session.get("http://127.0.0.1:8000/api/health", timeout=aiohttp.ClientTimeout(total=10)) as r:
                await r.read()
        return time.time() - start
    except Exception:
        return -1.0


async def check_containers() -> list[str]:
    issues = []
    try:
        async with aiohttp.ClientSession() as session:
            async with session.get("http://127.0.0.1:8000/api/health", timeout=aiohttp.ClientTimeout(total=5)) as r:
                data = await r.json()
                if data.get("status") != "ok":
                    issues.append("API返回非ok状态")
    except Exception as e:
        issues.append(f"API不可达: {e}")
    return issues


async def main():
    log("--- 系统资源监控开始 ---")
    state = load_state()
    new_state = {}

    # 1. 磁盘
    disk_pct, disk_free = check_disk()
    log(f"磁盘: usage={disk_pct:.1f}% free={disk_free}")
    if disk_pct >= DISK_THRESHOLD:
        if not state.get("disk_alert"):
            await send_alert("磁盘空间告警", f"磁盘使用率 {disk_pct:.1f}% (阈值 {DISK_THRESHOLD}%)\n剩余空间: {disk_free}")
        new_state["disk_alert"] = True
    else:
        if state.get("disk_alert"):
            await send_alert("磁盘空间恢复", f"磁盘使用率已降至 {disk_pct:.1f}%\n剩余空间: {disk_free}")

    # 2. 内存
    mem_pct, mem_avail, mem_total = check_memory()
    log(f"内存: usage={mem_pct:.1f}% avail={mem_avail}MB total={mem_total}MB")
    if mem_pct >= MEM_THRESHOLD:
        if not state.get("mem_alert"):
            await send_alert("内存使用告警", f"内存使用率 {mem_pct:.1f}% (阈值 {MEM_THRESHOLD}%)\n可用: {mem_avail}MB / 总计: {mem_total}MB")
        new_state["mem_alert"] = True
    else:
        if state.get("mem_alert"):
            await send_alert("内存使用恢复", f"内存使用率已降至 {mem_pct:.1f}%\n可用: {mem_avail}MB")

    # 3. CPU
    cpu_load = check_cpu()
    log(f"CPU: load1={cpu_load}")
    if cpu_load >= LOAD_THRESHOLD:
        if not state.get("cpu_alert"):
            await send_alert("CPU 负载告警", f"CPU 负载 {cpu_load} (阈值 {LOAD_THRESHOLD})")
        new_state["cpu_alert"] = True
    else:
        if state.get("cpu_alert"):
            await send_alert("CPU 负载恢复", f"CPU 负载已降至 {cpu_load}")

    # 4. API 延迟
    api_latency = await check_api_latency()
    log(f"API: latency={api_latency:.3f}s")
    if api_latency < 0:
        if not state.get("api_alert"):
            await send_alert("API 无响应告警", "后端 API 健康检查无响应,请检查容器状态")
        new_state["api_alert"] = True
    elif api_latency >= API_LATENCY_THRESHOLD:
        if not state.get("api_alert"):
            await send_alert("API 响应延迟告警", f"API 响应时间 {api_latency:.2f}s (阈值 {API_LATENCY_THRESHOLD}s)")
        new_state["api_alert"] = True
    else:
        if state.get("api_alert"):
            await send_alert("API 响应恢复", f"API 响应时间 {api_latency:.3f}s")

    # 5. 容器健康
    container_issues = await check_containers()
    if container_issues:
        if not state.get("container_alert"):
            await send_alert("容器健康告警", "\n".join(container_issues))
        new_state["container_alert"] = True
    else:
        if state.get("container_alert"):
            await send_alert("容器健康恢复", "所有服务运行正常")

    log("--- 系统资源监控完成 ---")
    save_state(new_state)


if __name__ == "__main__":
    asyncio.run(main())
