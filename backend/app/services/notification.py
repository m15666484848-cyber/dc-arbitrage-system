"""飞书 Webhook 告警服务。

支持:
- 飞书自定义机器人签名校验(webhook_secret 设置后携带 timestamp+sign)
- 告警去重:同一告警配置在 ALERT_DEDUP_SECONDS 内重复内容只发送一次,避免刷屏
- 彩色卡片头:根据事件类型自动匹配绿/蓝/金/红/橙/灰,更清晰易读
"""
from __future__ import annotations

import base64
import hashlib
import re
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
# 交易所错误消息中的毫秒/微秒时间戳(inTime/outTime/time)每次请求都不同,
# 若参与哈希会让去重永不命中,统一替换为占位符再计算指纹
_TS_RE = re.compile(r"\b1[6-9]\d{11,14}\b")
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


def _infer_level(event: str, title: str) -> str:
    """告警级别自动推断。

    P0=需立即处理(下单/平仓失败、欠费、急停) 红
    P1=交易动作与风控状态(开平仓、止盈止损触发、连亏暂停) 推送
    P2=信息性事件(挂单创建/过期、止盈止损更新、防重复跳过、业务拒绝) 静默落库+日报汇总
    """
    t = title or ""
    if event == "error":
        if "已拒绝" in t:
            return "P2"  # 业务拒绝(策略过期/风控拦截/参数不符)属正常流程
        return "P0"
    if event == "risk":
        return "P0" if "急停" in t else "P1"
    if event == "auth_expire":
        return "P1"
    if event == "correct":
        return "P2"
    if event == "warning":
        return "P1" if "余额" in t else "P2"
    if event == "tp_sl":
        quiet_kw = ("平仓成交", "止盈止损已更新", "成本保护", "追踪止损", "超72小时")
        return "P2" if any(k in t for k in quiet_kw) else "P1"
    if event == "order":
        quiet_kw = ("待触发", "已过期", "已撤销")
        return "P2" if any(k in t for k in quiet_kw) else "P1"
    return "P1"


def _alert_style(event: str, title: str, level: str = "P1", pnl: float | None = None) -> dict:
    """按级别+动作返回飞书卡片样式(表头直接用动作标题,颜色即语义)。"""
    t = title or ""
    if level == "P0":
        return {"header_bg": "red", "icon": "⛔"}
    if level == "P2":
        return {"header_bg": "grey", "icon": "📝"}
    if event == "risk":
        return {"header_bg": "orange", "icon": "🚨"}
    if event == "auth_expire":
        return {"header_bg": "grey", "icon": "🔑"}
    if event == "warning":
        return {"header_bg": "orange", "icon": "⚠️"}
    open_kw = ("开仓", "建仓", "进场", "开多", "开空")
    if any(k in t for k in open_kw):
        return {"header_bg": "green", "icon": "🟢"}
    if pnl is not None:
        if pnl > 0:
            return {"header_bg": "green", "icon": "✅"}
        if pnl < 0:
            return {"header_bg": "red", "icon": "🔻"}
        return {"header_bg": "blue", "icon": "➖"}
    if "止损" in t or "亏损" in t:
        return {"header_bg": "red", "icon": "🔻"}
    if "止盈" in t or "盈利" in t:
        return {"header_bg": "green", "icon": "✅"}
    if "失败" in t or event == "error":
        return {"header_bg": "red", "icon": "⛔"}
    return {"header_bg": "blue", "icon": "🔵"}


async def _send_feishu(
    webhook_url: str,
    title: str,
    content: str,
    event: str = "system",
    secret: str = "",
    level: str = "P1",
    pnl: float | None = None,
) -> tuple[bool, str]:
    """发送飞书互动卡片消息。设置 secret 时携带签名校验。"""
    style = _alert_style(event, title, level=level, pnl=pnl)
    now_str = _now_beijing_str()

    payload = {
        "msg_type": "interactive",
        "card": {
            "config": {"wide_screen_mode": True},
            "header": {
                "title": {
                    "tag": "plain_text",
                    "content": f"{style['icon']} {title}",
                },
                "template": style["header_bg"],
            },
            "elements": [
                {
                    "tag": "div",
                    "text": {
                        "tag": "lark_md",
                        "content": content,
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
    level: str = "",
    pnl: float | None = None,
) -> None:
    """按客户/全局告警配置发送飞书通知并落日志(使用独立事务,不影响调用方)。

    event: signal|order|tp_sl|correct|risk|auth_expire|error|warning
    source_text: 触发本次告警的原始 KOL 消息文本(可选,用于溯源)
    kol_name: KOL 名称(可选,自动添加到通知内容开头,便于识别信号来源)
    level: P0=需立即处理(红,推送) P1=交易动作/风控(推送) P2=信息性(静默落库,日报汇总);空=自动推断
    pnl: 平仓类盈亏(USDT),用于卡片颜色: 盈利绿/亏损红

    去重:同一 alert_config_id 在 ALERT_DEDUP_SECONDS 内的相同内容(title+content 哈希)
    只发送+记录一次,避免短时间内重复告警刷屏。
    """
    if not level:
        level = _infer_level(event, title)
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
    stable_content_for_hash = _TS_RE.sub("<TS>", stable_content_for_hash)
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
            # 事件开关对所有级别一致: 关闭的类型不推送、不落库、不进日报
            if flag_col is not None and not getattr(cfg, flag_col.key):
                continue
            # P2 静默: 只落库不发飞书(信息性事件不刷屏,由每日日报汇总)
            if level == "P2":
                db.add(AlertLog(
                    alert_config_id=cfg.id, event=event, title=title,
                    content=content, success=True, response="quiet", level=level,
                ))
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
                cfg.webhook_url, title, content, event=event,
                secret=cfg.webhook_secret or "", level=level, pnl=pnl,
            )
            # ★ 修复: 发送失败时清除去重标记,允许 60 秒内重试
            if not success and redis is not None:
                try:
                    await redis.delete(dedup_key)
                    logger.info(f"告警发送失败,已清除去重标记允许重试: cfg={cfg.id} event={event}")
                except Exception as e:
                    logger.opt(exception=True).warning(f"Unexpected error: {e}")
            log = AlertLog(
                alert_config_id=cfg.id,
                event=event,
                title=title,
                content=content,
                success=success,
                response=resp,
                level=level,
            )
            db.add(log)
        try:
            await db.commit()
        except Exception:
            await db.rollback()
            logger.exception("db commit failed")
            raise


async def send_quiet_digest() -> int:
    """每日P2静默事件汇总。窗口按配置独立(各自上次成功日报以来),发送失败不推进窗口、次日重发。"""
    from sqlalchemy import text as _text, bindparam

    flag_map = {
        "signal": AlertConfig.on_signal,
        "order": AlertConfig.on_order,
        "tp_sl": AlertConfig.on_tp_sl,
        "correct": AlertConfig.on_correct,
        "risk": AlertConfig.on_risk,
        "auth_expire": AlertConfig.on_auth_expire,
        "error": AlertConfig.on_error,
    }

    async with AsyncSessionLocal() as db:
        cfgs = (await db.execute(
            select(AlertConfig).where(
                AlertConfig.enabled.is_(True),
                AlertConfig.webhook_url.is_not(None),
            )
        )).scalars().all()

        sent_total = 0
        for cfg in cfgs:
            allowed = [ev for ev, col in flag_map.items() if getattr(cfg, col.key)]
            if not allowed:
                continue
            # F1: 窗口=该配置自身上次成功日报时间(失败不落digest行,故失败不推进窗口)
            last = (await db.execute(_text(
                "select max(created_at), "
                "to_char(max(created_at) at time zone 'Asia/Shanghai', 'MM-DD HH24:MI') "
                "from alert_logs where response = 'digest' and alert_config_id = :cfg_id"
            ), {"cfg_id": cfg.id})).fetchone()
            if last and last[0] is not None:
                start_sql, params, start_label = ":start_ts", {"start_ts": last[0]}, last[1]
            else:
                start_sql = ("date_trunc('day', now() at time zone 'Asia/Shanghai') "
                             "at time zone 'Asia/Shanghai'")
                params, start_label = {}, "今日 00:00"

            q = _text(
                "select regexp_replace(title, '[0-9]+(\\.[0-9]+)?', 'N', 'g') as t, count(*) as c, "
                "to_char(max(created_at) at time zone 'Asia/Shanghai', 'HH24:MI') as last_t "
                "from alert_logs "
                "where level = 'P2' and alert_config_id = :cfg_id and event in :events "
                f"and created_at >= {start_sql} "
                "group by 1 order by 2 desc limit 20"
            ).bindparams(bindparam("events", expanding=True))
            p = dict(params)
            p.update({"cfg_id": cfg.id, "events": allowed})
            rows = (await db.execute(q, p)).fetchall()
            if not rows:
                continue
            total = sum(r[1] for r in rows)
            lines = [f"• {r[0]} ×{r[1]}(最近{r[2]})" for r in rows]
            content = (
                f"自 {start_label} 以来共 {total} 条静默事件(P2级,未实时推送):\n"
                + "\n".join(lines)
                + "\n\n说明: P2为信息性事件(挂单创建/过期、止盈止损更新、成本保护、防重复跳过、业务拒绝等),"
                "已全部落库,明细可在告警日志中查询"
            )
            # F2: _send_feishu 失败时返回(False,resp)而非抛异常,必须解包判断
            try:
                ok, resp = await _send_feishu(
                    cfg.webhook_url, "静默事件日报", content,
                    event="signal", secret=cfg.webhook_secret or "", level="P1",
                )
            except Exception:
                logger.opt(exception=True).warning(f"日报发送异常 cfg={cfg.id}")
                ok, resp = False, "exception"
            if ok:
                db.add(AlertLog(
                    alert_config_id=cfg.id, event="signal", title="静默事件日报",
                    content=content, success=True, response="digest", level="P1",
                ))
                sent_total += total
            else:
                logger.warning(f"日报发送失败(窗口不前移,次日重发) cfg={cfg.id}: {str(resp)[:200]}")
                db.add(AlertLog(
                    alert_config_id=cfg.id, event="signal", title="静默事件日报(发送失败)",
                    content=content, success=False, response=str(resp)[:500], level="P1",
                ))
        await db.commit()
        return sent_total
