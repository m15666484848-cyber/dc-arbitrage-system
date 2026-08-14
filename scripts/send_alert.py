#!/usr/bin/env python3
"""通用飞书告警发送 — 供 shell 脚本调用
用法: python send_alert.py "标题" "内容"
"""
import asyncio, sys
sys.path.insert(0, "/app")
from app.services.notification import _send_feishu

WEBHOOKS = [
    "https://open.feishu.cn/open-apis/bot/v2/hook/eb8cfb4f-fd41-4d15-86c5-e29970500a9e",
    "https://open.feishu.cn/open-apis/bot/v2/hook/1ccf8fa3-46a9-46fa-817a-03829b8dc488",
]

async def main():
    title = sys.argv[1] if len(sys.argv) > 1 else "告警"
    content = sys.argv[2] if len(sys.argv) > 2 else ""
    for url in WEBHOOKS:
        ok, msg = await _send_feishu(url, title, content, "risk")
        if not ok:
            print(f"发送失败: {msg}", file=sys.stderr)

asyncio.run(main())
