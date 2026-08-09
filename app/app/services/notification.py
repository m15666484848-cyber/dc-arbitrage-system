"""飞书 Webhook 告警服务。"""
from __future__ import annotations

from loguru import logger
from sqlalchemy import select

from app.core.database import AsyncSessionLocal
from app.models.audit import AlertLog
from app.models.config import AlertConfig
from app.services.llm_client import get_httpx_client


async def _send_feishu(webhook_url: str, title: str, content: str) -> tuple[bool, str]:
    """发送飞书互动卡片消息。"""
    payload = {
        "msg_type": "interactive",
        "card": {
            "header": {
                "title": {"tag": "plain_text", "content": title},
                "template": "red" if "错误" in title or "熔断" in title or "失败" in title else "blue",
            },
            "elements": [
                {"tag": "div", "text": {"tag": "lark_md", "content": content}},
            ],
        },
    }
    try:
        client = get_httpx_client()
        resp = await client.post(webhook_url, json=payload)
        return resp.status_code == 200 and resp.json().get("code", 0) == 0, resp.text[:500]
    except Exception as e:
        logger.error(f"飞书告警发送失败: {e}")
        return False, str(e)


async def notify(
    event: str,
    title: str,
    content: str,
    customer_id: int | None = None,
) -> None:
    """按客户/全局告警配置发送飞书通知并落日志(使用独立事务,不影响调用方)。

    event: signal|order|tp_sl|correct|risk|auth_expire|error
    """
    flag_map = {
        "signal": AlertConfig.on_signal,
        "order": AlertConfig.on_order,
        "tp_sl": AlertConfig.on_tp_sl,
        "correct": AlertConfig.on_correct,
        "risk": AlertConfig.on_risk,
        "auth_expire": AlertConfig.on_auth_expire,
        "error": AlertConfig.on_error,
    }
    flag_col = flag_map.get(event)

    async with AsyncSessionLocal() as db:
        stmt = select(AlertConfig).where(AlertConfig.enabled.is_(True))
        if customer_id is not None:
            stmt = stmt.where((AlertConfig.customer_id == customer_id) | (AlertConfig.customer_id.is_(None)))
        else:
            stmt = stmt.where(AlertConfig.customer_id.is_(None))
        result = await db.execute(stmt)
        configs = result.scalars().all()

        for cfg in configs:
            if flag_col is not None and not getattr(cfg, flag_col.key):
                continue
            if not cfg.webhook_url:
                logger.warning(f"告警配置 {cfg.id} webhook_url 为空,跳过发送")
                continue
            success, resp = await _send_feishu(cfg.webhook_url, title, content)
            log = AlertLog(
                alert_config_id=cfg.id,
                event=event,
                title=title,
                content=content,
                success=success,
                response=resp,
            )
            db.add(log)
        await db.commit()
