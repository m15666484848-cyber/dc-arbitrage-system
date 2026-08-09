"""Discord Gateway 监听服务(用户 TOKEN 直连 WebSocket)。

流程:获取 gateway URL → 连接 → IDENTIFY → 心跳循环 → 监听 MESSAGE_CREATE
→ 按 channel_id 路由到 KOL → 解析信号(支持 KOL 级别 LLM 配置) → 落库 → 分发给所有关注该 KOL 的客户处理。

注:用户 TOKEN 属 self-bot,违反 Discord ToS,仅用于监听已加入的付费 KOL 群。
"""
from __future__ import annotations

import asyncio
import json
from datetime import datetime, timezone

import websockets
from loguru import logger
from sqlalchemy import select

from app.core.config import settings
from app.core.database import AsyncSessionLocal
from app.models.kol import Kol, KolFollow
from app.models.signal import Signal
from app.services import order_manager, signal_parser
from app.services.signal_parser import KolLLMConfig
from app.services.event_bus import bus


GATEWAY_URL = "wss://gateway.discord.gg/?v=10&encoding=json"

# 信号处理并发数和超时(从环境变量读取,支持运行时调优)
from app.core.config import settings as _cfg
SIGNAL_SEMAPHORE = asyncio.Semaphore(getattr(_cfg, 'discord_signal_concurrency', 10))
PROCESS_TIMEOUT = getattr(_cfg, 'discord_process_timeout', 120)
_customer_locks: dict[int, asyncio.Lock] = {}
MAX_RECONNECT_ATTEMPTS = 20


async def _get_gateway() -> str:
    return GATEWAY_URL


async def _handle_message(payload: dict) -> None:
    """处理一条 MESSAGE_CREATE 事件。"""
    data = payload.get("d", {})
    channel_id = data.get("channel_id", "")
    message_id = data.get("id", "")
    author = data.get("author", {})
    author_id = author.get("id", "")
    content = data.get("content", "") or ""
    # 图片附件
    image_url = ""
    for att in data.get("attachments", []):
        if att.get("content_type", "").startswith("image"):
            image_url = att.get("url", "")
            break
    # embed 图片
    if not image_url:
        for emb in data.get("embeds", []):
            img = emb.get("image") or emb.get("thumbnail")
            if img and img.get("url"):
                image_url = img["url"]
                break

    async with AsyncSessionLocal() as db:
        # 找到该频道对应的 KOL
        stmt = select(Kol).where(Kol.discord_channel_id == channel_id, Kol.enabled.is_(True))
        kol = (await db.execute(stmt)).scalar_one_or_none()
        if not kol:
            return  # 非监听频道
        # 若 KOL 指定了 user_id,则只处理该用户消息
        if kol.discord_user_id and author_id != kol.discord_user_id:
            return

        # 获取 KOL 级别 LLM 配置
        kol_llm_config = KolLLMConfig.from_kol(kol)

        # 日志：记录 LLM 配置状态
        if kol_llm_config.enabled:
            logger.debug(
                f"KOL {kol.name} 启用 LLM: "
                f"vision_enabled={kol_llm_config.vision_enabled}, "
                f"fallback={kol_llm_config.fallback}, "
                f"min_confidence={kol_llm_config.min_confidence}"
            )

        # 解析信号（传入 KOL 配置）
        parsed = await signal_parser.parse_message(
            content,
            image_url,
            kol_config=kol_llm_config,
            kol_name=kol.name,
        )

        now = datetime.now(timezone.utc)
        signal = Signal(
            kol_id=kol.id,
            discord_message_id=message_id,
            raw_text=content,
            image_url=image_url,
            parsed=parsed.model_dump(),
            status="received" if (parsed.symbol or parsed.side or parsed.is_exit_signal) else "ignored",
            received_at=now,
            symbol=parsed.symbol,
            side=parsed.side,
            entry_price=parsed.entry_price,
            confidence=parsed.confidence,
        )
        db.add(signal)
        await db.commit()
        await db.refresh(signal)

        # 广播信号事件(管理员/前端可见)
        await bus.publish("admin", "signal", {
            "id": signal.id, "kol_id": kol.id, "kol_name": kol.name,
            "symbol": parsed.symbol, "side": parsed.side, "raw_text": content[:200],
            "status": signal.status, "confidence": parsed.confidence,
        })

        if signal.status == "ignored":
            return

        follows = (
            await db.execute(
                select(KolFollow).where(KolFollow.kol_id == kol.id, KolFollow.enabled.is_(True))
            )
        ).scalars().all()
        for follow in follows:
            asyncio.create_task(
                _process_for_customer_sem(signal.id, follow.customer_id)
            )


async def _process_for_customer_sem(signal_id: int, customer_id: int) -> None:
    """信号处理包装:并发控制 + 超时保护 + per-customer 锁。"""
    async with SIGNAL_SEMAPHORE:
        lock = _customer_locks.setdefault(customer_id, asyncio.Lock())
        async with lock:
            try:
                await asyncio.wait_for(
                    _process_for_customer(signal_id, customer_id),
                    timeout=PROCESS_TIMEOUT,
                )
            except asyncio.TimeoutError:
                logger.error(f"客户 {customer_id} 处理信号 {signal_id} 超时({PROCESS_TIMEOUT}s)")
            except Exception as e:
                logger.exception(f"客户 {customer_id} 处理信号 {signal_id} 失败: {e}")


async def _process_for_customer(signal_id: int, customer_id: int) -> None:
    """为单个客户处理信号(独立会话)。"""
    async with AsyncSessionLocal() as db:
        signal = (await db.execute(select(Signal).where(Signal.id == signal_id))).scalar_one_or_none()
        if not signal:
            return
        from app.schemas.signal import ParsedSignal

        parsed = ParsedSignal(**(signal.parsed or {}))
        await order_manager.process_signal(db, signal, parsed, customer_id)


async def _heartbeat_with_ack(ws, interval: int, seq: list[int], last_ack: list[float]) -> None:
    """心跳循环,检测超时则退出触发重连。"""
    while True:
        try:
            await asyncio.sleep(interval / 1000.0)
            await ws.send(json.dumps({"op": 1, "d": seq[0]}))
            
            # 检测心跳超时(超过 2.5 个间隔未收到 ACK)
            now = asyncio.get_event_loop().time()
            if now - last_ack[0] > (interval * 2.5 / 1000.0):
                logger.error("Discord 心跳超时,触发重连")
                return  # 退出心跳循环,触发外层重连
                
        except Exception as e:
            logger.error(f"Discord 心跳异常: {e}")
            return  # 退出心跳循环,触发外层重连


async def run_discord_monitor() -> None:
    """Discord Gateway 主循环(断线自动重连 + Token 热重载)。

    每 60 秒检查一次 Token 是否变化(管理页修改后),变了则主动断开重连。
    """
    from app.core.runtime_config import get_discord_settings

    discord_cfg = await get_discord_settings()
    if not discord_cfg.token:
        logger.warning("未配置 DISCORD_TOKEN(数据库和 .env 均为空),Discord 监听未启动")
        return

    token = discord_cfg.token
    token_hash = hash(token)
    reconnect_count = 0
    MAX_RECONNECT_BACKOFF = 60
    TOKEN_CHECK_INTERVAL = 60
    consecutive_failures = 0

    while True:
        try:
            if consecutive_failures >= MAX_RECONNECT_ATTEMPTS:
                logger.error(
                    f"Discord 重连失败已达 {MAX_RECONNECT_ATTEMPTS} 次, "
                    f"等待 5 分钟后重置计数重试"
                )
                await asyncio.sleep(300)
                consecutive_failures = 0

            gateway = await _get_gateway()
            async with websockets.connect(gateway, max_size=None) as ws:
                # Hello
                hello = json.loads(await ws.recv())
                heartbeat_interval = hello.get("d", {}).get("heartbeat_interval", 41250)
                seq: list[int] = [0]
                last_ack_ref = [asyncio.get_event_loop().time()]  # 心跳 ACK 时间戳
                asyncio.create_task(_heartbeat_with_ack(ws, heartbeat_interval, seq, last_ack_ref))

                # Identify
                await ws.send(json.dumps({
                    "op": 2,
                    "d": {
                        "token": token,
                        "intents": 512 | 32768,  # GUILD_MESSAGES | MESSAGE_CONTENT
                        "properties": {"os": "linux", "browser": "dcquant", "device": "dcquant"},
                    },
                }))

                logger.info("Discord Gateway 已连接,开始监听 KOL 消息")
                reconnect_count = 0
                consecutive_failures = 0

                # 并发:监听消息 + 定期检查 Token 变化
                import asyncio as _aio
                async def _watch_token_change():
                    """定期检查 Token,变了就主动断开重连。"""
                    while True:
                        await _aio.sleep(TOKEN_CHECK_INTERVAL)
                        try:
                            new_cfg = await get_discord_settings()
                            if hash(new_cfg.token) != token_hash:
                                logger.info("检测到 Discord Token 变化,触发重连")
                                await ws.close()
                                return
                        except Exception as e:
                            logger.debug(f"Token 检查异常: {e}")

                watcher = _aio.create_task(_watch_token_change())

                try:
                    async for raw in ws:
                        payload = json.loads(raw)
                        op = payload.get("op")
                        if op == 0:  # Dispatch
                            seq[0] = payload.get("s", seq[0])
                            if payload.get("t") == "MESSAGE_CREATE":
                                asyncio.create_task(_handle_message(payload))
                        elif op == 11:  # Heartbeat ACK
                            last_ack_ref[0] = asyncio.get_event_loop().time()
                        elif op == 7:  # Reconnect
                            logger.info("Discord 要求重连")
                            break
                finally:
                    watcher.cancel()

        except Exception as e:
            reconnect_count += 1
            consecutive_failures += 1
            backoff = min(5 * (2 ** min(reconnect_count, 4)), MAX_RECONNECT_BACKOFF)
            logger.exception(
                f"Discord 连接异常: {e}, {backoff} 秒后第 {reconnect_count} 次重连"
            )
            await asyncio.sleep(backoff)

        # 重连前重新读取 Token(可能已更新)
        try:
            discord_cfg = await get_discord_settings()
            if hash(discord_cfg.token) != token_hash:
                token = discord_cfg.token
                token_hash = hash(token)
                logger.info("Discord Token 已更新,使用新 Token 重连")
        except Exception:
            pass
