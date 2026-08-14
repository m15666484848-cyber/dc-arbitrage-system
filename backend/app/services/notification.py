"""飞书 Webhook 告警服务。

支持:
- 飞书自定义机器人签名校验(webhook_secret 设置后携带 timestamp+sign)
- 告警去重:同一告警配置在 ALERT_DEDUP_SECONDS 内重复内容只发送一次,避免刷屏
- 彩色卡片头:根据事件类型自动匹配绿/蓝/金/红/橙/灰,更清晰易读
"""
from __future__ import annotations

import base64
import hashlib
import hmac
import time
from datetime import datetime, timezone, timedelta

from loguru import logger
from sqlalchemy import select

from app.core.database import AsyncSessionLocal
from app.core.redis import get_redis
from app.models.audit import AlertLog
from app.models.config import AlertConfig
from app.services.llm_client import get_httpx_client

# 同一告警配置的去重窗口(秒):窗口内相同内容的告警只发送+记录一次
ALERT_DEDUP_SECONDS = 60
_BEIJING_TZ = timezone(timedelta(hours=8))


def _now_beijing_str() -> str:
    """返回当前北京时间字符串,格式: 2026-08-06 19:30:25"""
    return datetime.now(_BEIJING_TZ).strftime("%Y-%m-%d %H:%M:%S")


def _feishu_sign(secret: str) -> tuple[str, str]:
    """生成飞书自定义机器人签名。

    算法(官方):string_to_sign = f"{timestamp}\n{secret}",
    sign = base64(hmac_sha256(string_to_sign)),返回 (timestamp, sign)。
    """
    timestamp = str(int(time.time()))
    string_to_sign = f"{timestamp}\n{secret}"
    hmac_code = hmac.new(
        string_to_sign.encode("utf-8"), digestmod=hashlib.sha256
    ).digest()
    sign = base64.b64encode(hmac_code).decode("utf-8")
    return timestamp, sign


def _alert_style(event: str, title: str) -> dict:
    """根据事件和标题返回飞书卡片样式。"""
    t = title or ""
    lower = t.lower()

    # 先按标题语义判断(更精确)
    if "止盈" in t or "盈利" in t or "profit" in lower:
        return {"header_bg": "yellow", "header_title": "💰 止盈平仓", "icon": "🎉"}
    if "止损" in t and ("触发" in t or "平仓" in t or "亏损" in t):
        return {"header_bg": "red", "header_title": "⚠️ 止损平仓", "icon": "💥"}
    if "开仓" in t or "建仓" in t or "跟单下单" in t:
        return {"header_bg": "green", "header_title": "🟢 开仓信号", "icon": "📈"}
    if "平仓" in t:
        return {"header_bg": "blue", "header_title": "🔵 平仓信号", "icon": "📉"}
    if "风险" in t or "警告" in t or "告警" in t or "跳过" in t or "超限" in t:
        return {"header_bg": "orange", "header_title": "⚠️ 风控警告", "icon": "🚨"}
    if "错误" in t or "失败" in t or "异常" in t or "熔断" in t:
        return {"header_bg": "red", "header_title": "❌ 系统错误", "icon": "⛔"}
    if "纠错" in t or "修正" in t:
        return {"header_bg": "blue", "header_title": "🔧 信号纠错", "icon": "✏️"}
    if "挂单" in t or "等待" in t:
        return {"header_bg": "green", "header_title": "🟢 挂单等待", "icon": "⏳"}

    # 再按事件类型兜底
    event_map = {
        "signal": {"header_bg": "blue", "header_title": "📩 信号通知", "icon": "📩"},
        "order": {"header_bg": "green", "header_title": "🟢 订单执行", "icon": "📈"},
        "tp_sl": {"header_bg": "yellow", "header_title": "💰 止盈止损", "icon": "🎯"},
        "correct": {"header_bg": "blue", "header_title": "🔧 信号纠错", "icon": "✏️"},
        "risk": {"header_bg": "orange", "header_title": "⚠️ 风控警告", "icon": "🚨"},
        "auth_expire": {"header_bg": "grey", "header_title": "🔑 授权到期", "icon": "🔔"},
        "error": {"header_bg": "red", "header_title": "❌ 系统错误", "icon": "⛔"},
    }
    return event_map.get(event, {"header_bg": "grey", "header_title": "🔔 系统通知", "icon": "🔔"})


async def _send_feishu(
    webhook_url: str,
    title: str,
    content: str,
    event: str = "system",
    secret: str = "",
) -> tuple[bool, str]:
    """发送飞书互动卡片消息。设置 secret 时携带签名校验。"""
    style = _alert_style(event, title)
    now_str = _now_beijing_str()

    payload = {
        "msg_type": "interactive",
        "card": {
            "config": {"wide_screen_mode": True},
            "header": {
                "title": {
                    "tag": "plain_text",
                    "content": title if style["header_title"] == "🟢 开仓信号" else f"{style['icon']} {style['header_title']}",
                },
                "template": style["header_bg"],
            },
            "elements": [
                {
                    "tag": "div",
                    "text": {
                        "tag": "lark_md",
                        "content": f"**{title}**\n\n{content}",
                    },
                },
                {"tag": "hr"},
                {
                    "tag": "note",
                    "elements": [
                        {
                            "tag": "plain_text",
                            "content": f"🕐 {now_str}  |  DCQuant KOL 跟单系统",
                        }
                    ],
                },
            ],
        },
    }
    # 签名校验:机器人安全设置开启"签名校验"后必须携带 timestamp+sign
    if secret:
        ts, sign = _feishu_sign(secret)
        payload["timestamp"] = ts
        payload["sign"] = sign
    try:
        client = get_httpx_client()
        resp = await client.post(webhook_url, json=payload)
        return resp.status_code == 200 and resp.json().get("code", 0) == 0, resp.text[:500]
    except Exception as e:
        logger.error(f"飞书告警发送失败: {e}")
        return False, str(e)


async def _is_duplicate(redis, dedup_key: str) -> bool:
    """检查是否为重复告警。首次出现则写入标记并返回 False,重复则返回 True。"""
    # SET NX:仅在 key 不存在时设置,返回 True 表示写入成功(非重复)
    added = await redis.set(dedup_key, "1", ex=ALERT_DEDUP_SECONDS, nx=True)
    return not added


async def notify(
    event: str,
    title: str,
    content: str,
    customer_id: int | None = None,
    source_text: str = "",
    kol_name: str = "",
) -> None:
    """按客户/全局告警配置发送飞书通知并落日志(使用独立事务,不影响调用方)。

    event: signal|order|tp_sl|correct|risk|auth_expire|error
    source_text: 触发本次告警的原始 KOL 消息文本(可选,用于溯源)
    kol_name: KOL 名称(可选,自动添加到通知内容开头,便于识别信号来源)

    去重:同一 alert_config_id 在 ALERT_DEDUP_SECONDS 内的相同内容(title+content 哈希)
    只发送+记录一次,避免短时间内重复告警刷屏。
    """
    # ★ 自动添加 KOL 名称到通知内容开头(如果尚未包含)
    if kol_name and "KOL" not in content[:50]:
        content = f"👤 KOL: {kol_name}\n{content}"
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

    if customer_id is not None and not isinstance(customer_id, int):
        try:
            customer_id = int(customer_id)
        except (TypeError, ValueError):
            logger.warning("告警 customer_id 非法,已按全局告警处理: %r", customer_id)
            customer_id = None

    # 内容指纹用于去重(与配置 id 组合)。必须在追加时间戳前计算,否则每次内容都不同。
    stable_content_for_hash = content
    if source_text:
        stable_content_for_hash += f"\n原始消息: {source_text.strip()[:200]}"
    content_hash = hashlib.sha256(f"{title}|{stable_content_for_hash}".encode("utf-8")).hexdigest()[:16]

    # 自动追加告警时间和原始消息(方便后期检查)
    _time_str = _now_beijing_str()
    _enriched = f"\n---\n告警时间: {_time_str}"
    if source_text:
        # 截取原始消息前200字,避免过长
        _src = source_text.strip()[:200]
        _enriched += f"\n原始消息: {_src}"
    content = content + _enriched

    try:
        redis = await get_redis()
    except Exception as e:
        logger.warning(f"获取 Redis 失败,告警去重降级为不去重: {e}")
        redis = None

    async with AsyncSessionLocal() as db:
        stmt = select(AlertConfig).where(AlertConfig.enabled.is_(True))
        if customer_id is not None:
            stmt = stmt.where(
                (AlertConfig.customer_id == customer_id) | (AlertConfig.customer_id.is_(None))
            )
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

            # 去重:同一配置相同内容在窗口内只发一次
            dedup_key = f"alert_dedup:{cfg.id}:{content_hash}"
            if redis is not None and await _is_duplicate(redis, dedup_key):
                logger.debug(f"告警去重命中,跳过: cfg={cfg.id} event={event} title={title}")
                continue

            success, resp = await _send_feishu(
                cfg.webhook_url, title, content, event=event, secret=cfg.webhook_secret or ""
            )
            # ★ 修复: 发送失败时清除去重标记,允许 60 秒内重试
            if not success and redis is not None:
                try:
                    await redis.delete(dedup_key)
                    logger.info(f"告警发送失败,已清除去重标记允许重试: cfg={cfg.id} event={event}")
                except Exception:
                    pass
            log = AlertLog(
                alert_config_id=cfg.id,
                event=event,
                title=title,
                content=content,
                success=success,
                response=resp,
            )
            db.add(log)
        try:
            await db.commit()
        except Exception:
            await db.rollback()
            logger.exception("db commit failed")
            raise
