"""Discord Gateway 监听服务(用户 TOKEN 直连 WebSocket)。





流程:获取 gateway URL → 连接 → IDENTIFY → 心跳循环 → 监听 MESSAGE_CREATE


→ 按 channel_id 路由到 KOL → 解析信号(支持 KOL 级别 LLM 配置) → 落库 → 分发给所有关注该 KOL 的客户处理。





注:用户 TOKEN 属 self-bot,违反 Discord ToS,仅用于监听已加入的付费 KOL 群。


"""


from __future__ import annotations





import re





import asyncio


import json
import os


from datetime import datetime, timezone, timedelta





import websockets


from loguru import logger


from sqlalchemy import or_, select



from app.core.config import settings


from app.core.database import AsyncSessionLocal


from app.models.config import DiscordAccount
from app.models.kol import Kol, KolFollow


from app.models.signal import Signal
from app.models.trading import Position


from app.services import order_manager, signal_parser


from app.services.signal_parser import KolLLMConfig


from app.services.event_bus import bus








GATEWAY_URL = "wss://gateway.discord.gg/?v=10&encoding=json"





from app.core.config import settings as _cfg


SIGNAL_SEMAPHORE = asyncio.Semaphore(getattr(_cfg, 'discord_signal_concurrency', 10))
PROCESS_TIMEOUT = getattr(_cfg, 'discord_process_timeout', 120)
_message_semaphore = asyncio.Semaphore(20)  # MESSAGE_CREATE 并发限制
_customer_locks: dict[int, asyncio.Lock] = {}
_pending_tasks: set[asyncio.Task] = set()  # 防止 task 被 GC 回收
MAX_RECONNECT_ATTEMPTS = 20
RAW_TEXT_DEDUP_WINDOW = timedelta(minutes=5)
_recent_raw_text_seen: dict[tuple[int, str], datetime] = {}


_SOURCE_STATUS: dict = {
    "source": "Discord",
    "state": "starting",
    "connected": False,
    "configured": False,
    "last_connected_at": None,
    "last_heartbeat_sent_at": None,
    "last_heartbeat_ack_at": None,
    "last_message_at": None,
    "last_kol_message_at": None,
    "last_channel_id": "",
    "last_kol_name": "",
    "last_error": "",
    "reconnect_count": 0,
    "consecutive_failures": 0,
    "session_id": "",
    "updated_at": None,
}


def _iso_now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _set_source_status(**kwargs) -> None:
    _SOURCE_STATUS.update(kwargs)
    _SOURCE_STATUS["updated_at"] = _iso_now()


def get_source_status() -> dict:
    """返回 Discord 转发源运行状态,供管理端展示。"""
    out = dict(_SOURCE_STATUS)
    out["healthy"] = bool(out.get("configured") and out.get("connected") and out.get("last_heartbeat_ack_at"))
    return out


def _is_duplicate_raw_text(kol_id: int, raw_text: str, now: datetime) -> bool:
    """5 分钟内同一 KOL 的相同原文只处理一次,避免重复入库/重复下单。"""
    normalized = (raw_text or "").strip()
    if not normalized:
        return False
    expire_before = now - RAW_TEXT_DEDUP_WINDOW
    for key, seen_at in list(_recent_raw_text_seen.items()):
        if seen_at < expire_before:
            _recent_raw_text_seen.pop(key, None)
    key = (kol_id, normalized)
    last_seen = _recent_raw_text_seen.get(key)
    if last_seen and last_seen >= expire_before:
        return True
    _recent_raw_text_seen[key] = now
    return False


async def _has_recent_duplicate_raw_text_in_db(db, kol_id: int, raw_text: str, now: datetime) -> bool:
    """数据库层去重: 防止进程重启后内存去重失效,导致异常/重复信号重复入库。"""
    normalized = (raw_text or "").strip()
    if not normalized:
        return False
    expire_before = now - RAW_TEXT_DEDUP_WINDOW
    existing = (
        await db.execute(
            select(Signal.id)
            .where(
                Signal.kol_id == kol_id,
                Signal.raw_text == normalized,
                Signal.received_at >= expire_before,
            )
            .order_by(Signal.received_at.desc())
            .limit(1)
        )
    ).scalar_one_or_none()
    return existing is not None


def _write_parser_diff_log(
    *,
    kol_id: int | None,
    kol_name: str,
    channel_id: str,
    message_id: str,
    raw_text: str,
    image_url: str,
    signal_id: int | None,
    parsed,
    status: str,
    reason: str,
    extra: dict | None = None,
) -> None:
    """写入独立解析差异日志(JSONL),用于和朋友服务器执行结果做离线对比。"""
    try:
        log_path = os.getenv("PARSER_DIFF_LOG_PATH", "/app/app/logs/parser_diff.log")
        os.makedirs(os.path.dirname(log_path), exist_ok=True)
        parsed_data = parsed.model_dump() if hasattr(parsed, "model_dump") else {}
        row = {
            "ts": _iso_now(),
            "source": "dcquant",
            "kol_id": kol_id,
            "kol_name": kol_name,
            "channel_id": channel_id,
            "message_id": message_id,
            "signal_id": signal_id,
            "raw_text": raw_text,
            "has_image": bool(image_url),
            "status": status,
            "reason": reason,
            "actions": parsed_data.get("actions", []),
            "action": parsed_data.get("action", ""),
            "symbol": parsed_data.get("symbol", ""),
            "side": parsed_data.get("side", ""),
            "entry_price": parsed_data.get("entry_price"),
            "entry_prices": parsed_data.get("entry_prices", []),
            "take_profits": parsed_data.get("take_profits", []),
            "stop_loss": parsed_data.get("stop_loss"),
            "condition_price": parsed_data.get("condition_price"),
            "confidence": parsed_data.get("confidence", 0.0),
            "is_exit_signal": parsed_data.get("is_exit_signal", False),
            "is_update_signal": parsed_data.get("is_update_signal", False),
            "parser_reason": parsed_data.get("reason", "") or parsed_data.get("exit_reason", "") or parsed_data.get("update_reason", ""),
            "extra": extra or {},
        }
        if os.path.exists(log_path) and os.path.getsize(log_path) > 50 * 1024 * 1024:
            rotated_path = f"{log_path}.1"
            if os.path.exists(rotated_path):
                os.remove(rotated_path)
            os.replace(log_path, rotated_path)
        with open(log_path, "a", encoding="utf-8") as f:
            f.write(json.dumps(row, ensure_ascii=False, separators=(",", ":")) + "\n")
    except Exception as e:
        logger.warning(f"写入解析差异日志失败: {e}")








async def _get_gateway() -> str:


    return GATEWAY_URL








async def _handle_message(
    payload: dict,
    discord_account_id: int | None = None,
    is_default_account: bool = False,
) -> None:
    """处理一条 MESSAGE_CREATE 事件。"""


    data = payload.get("d", {})


    channel_id = data.get("channel_id", "")


    message_id = data.get("id", "")


    author = data.get("author", {})


    author_id = author.get("id", "")


    content = data.get("content", "") or ""
    _set_source_status(last_message_at=_iso_now(), last_channel_id=channel_id)


    # 图片附件


    image_url = ""


    for att in data.get("attachments", []):


        ct = att.get("content_type", "")


        url = att.get("url", "") or att.get("proxy_url", "")


        if ct.startswith("image") or (url and re.search(r"\.(jpg|jpeg|png|gif|webp|bmp)(\?|$)", url, re.IGNORECASE)):


            image_url = url


            break


    # embed 图片


    if not image_url:


        for emb in data.get("embeds", []):


            img = emb.get("image") or emb.get("thumbnail")


            if img and img.get("url"):


                image_url = img["url"]


                break


    # 兜底: 消息内容中有图片链接时提取


    if not image_url and content:


        _m = re.search(r"(https?://\S+\.(?:jpg|jpeg|png|gif|webp|bmp)(?:\?\S*)?)", content, re.IGNORECASE)


        if _m:


            image_url = _m.group(1)





    # P2 修复: 将 DB 会话拆分为两段,避免 LLM 调用期间长时间占用 DB 连接
    # Phase 1: 查询 KOL 和历史信号(需要 DB 会话)
    async with AsyncSessionLocal() as db:
        # 找到该频道对应的 KOL,并按 Discord 账号绑定路由。
        # 兼容旧数据:未绑定 discord_account_id 的 KOL 只由默认账号处理。
        stmt = select(Kol).where(Kol.discord_channel_id == channel_id, Kol.enabled.is_(True))
        if discord_account_id is None:
            stmt = stmt.where(Kol.discord_account_id.is_(None))
        elif is_default_account:
            stmt = stmt.where(
                or_(
                    Kol.discord_account_id == discord_account_id,
                    Kol.discord_account_id.is_(None),
                )
            )
        else:
            stmt = stmt.where(Kol.discord_account_id == discord_account_id)
        kol = (await db.execute(stmt.limit(1))).scalar_one_or_none()
        if not kol:
            return  # 非监听频道
        _set_source_status(last_kol_message_at=_iso_now(), last_kol_name=kol.name)
        # 若 KOL 指定了 user_id,则只处理该用户消息
        if kol.discord_user_id and author_id != kol.discord_user_id:
            return

        # 非图片分析KOL:直接跳过图片处理,不做任何分析
        if not getattr(kol, 'vision_llm_enabled', False) and image_url:
            logger.debug(f"KOL {kol.name} 未启用图片分析,跳过图片: {image_url[:80]}")
            image_url = ""
            # 同时清理 content 中的图片 URL (避免后续被当作文本信号)
            if content:
                content = re.sub(
                    r'https?://\S+\.(?:jpg|jpeg|png|gif|webp|bmp)(?:\?\S*)?',
                    '',
                    content,
                    flags=re.IGNORECASE
                ).strip()

        now = datetime.now(timezone.utc)
        if _is_duplicate_raw_text(kol.id, content, now):
            logger.info(f"5分钟内重复 KOL 消息已跳过: kol={kol.name} message_id={message_id} text={content[:120]}")
            return

        # message_id 去重:Discord RESUME/重连后会重放事件,同一 message_id 不能重复入库
        # (否则会重复下单)。检查是否已存在该消息。
        if message_id:
            existing = (
                await db.execute(
                    select(Signal.id).where(Signal.discord_message_id == message_id).limit(1)
                )
            ).scalar_one_or_none()
            if existing:
                logger.debug(f"Discord 消息已处理过,跳过: message_id={message_id}")
                return

        # raw_text 数据库窗口去重:防止服务重启/重连后内存去重丢失,
        # 或 Discord 转发机器人使用不同 message_id 重复投递同一异常信号。
        if await _has_recent_duplicate_raw_text_in_db(db, kol.id, content, now):
            logger.info(
                f"5分钟内数据库已有相同 KOL 原文,跳过重复信号: "
                f"kol={kol.name} message_id={message_id} text={content[:120]}"
            )
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

        # 查询该KOL最近10条有效信号，作为上下文注入LLM（含原始文本+时间+持仓）
        _llm_context = ""
        _recent_texts: list[str] = []
        try:
            hist_stmt = (
                select(Signal)
                .where(
                    Signal.kol_id == kol.id,
                    Signal.status.in_(["received", "ordered", "rejected"]),
                )
                .order_by(Signal.received_at.desc())
                .limit(10)
            )
            hist_signals = (await db.execute(hist_stmt)).scalars().all()
            if hist_signals:
                _recent_texts = [h.raw_text for h in reversed(hist_signals) if h.raw_text]
                nums = ["①", "②", "③", "④", "⑤", "⑥", "⑦", "⑧", "⑨", "⑩"]
                lines = []
                for i, h in enumerate(reversed(hist_signals)):
                    parts = []
                    h_parsed = h.parsed or {}
                    # 时间信息（让 LLM 知道信号是多久以前的）
                    try:
                        t_str = h.received_at.strftime("%m-%d %H:%M") if h.received_at else ""
                    except Exception:
                        t_str = ""
                    # 结构化字段
                    if h_parsed.get("is_exit_signal"):
                        parts.append("平仓")
                        if h.symbol:
                            parts.append(h.symbol)
                    else:
                        if h_parsed.get("side"):
                            parts.append("做多" if h_parsed["side"] == "long" else "做空")
                        if h.symbol:
                            parts.append(h.symbol)
                        if h_parsed.get("entry_price"):
                            parts.append(f"进场{h_parsed['entry_price']}")
                        if h_parsed.get("stop_loss"):
                            parts.append(f"止损{h_parsed['stop_loss']}")
                        if h_parsed.get("take_profits"):
                            tps = h_parsed["take_profits"]
                            if isinstance(tps, list) and tps:
                                parts.append(f"止盈{','.join(str(t) for t in tps[:3])}")
                    # 信号状态（让 LLM 知道是否已成交）
                    sig_status = h.status or ""
                    if sig_status == "ordered":
                        parts.append("[已成交]")
                    elif sig_status == "rejected":
                        parts.append("[已拒绝]")
                    else:
                        parts.append("[待处理]")
                    # 原始文本前 120 字（让 LLM 看到 KOL 原话，关联口语化引用）
                    raw_snippet = (h.raw_text or "").replace("\n", " ").strip()[:120]
                    line = f"{nums[i] if i < len(nums) else f'({i+1})'} {' '.join(parts)}"
                    if t_str:
                        line += f" ({t_str})"
                    if raw_snippet:
                        line += f" 原文: {raw_snippet}"
                    lines.append(line)
                _llm_context = "[该KOL历史信号]\n" + "\n".join(lines)

                # 注入当前持仓状态（让 LLM 知道客户持有什么仓位）
                try:
                    from app.models.trading import Position
                    pos_stmt = (
                        select(Position)
                        .where(
                            Position.kol_id == kol.id,
                            Position.status == "open",
                        )
                        .order_by(Position.opened_at.desc())
                        .limit(10)
                    )
                    open_positions = (await db.execute(pos_stmt)).scalars().all()
                    if open_positions:
                        pos_lines = []
                        for p in open_positions:
                            p_side = "多" if p.side == "long" else "空"
                            p_info = f"  {p.symbol} {p_side}仓 数量={p.qty} 开仓价={p.entry_price}"
                            if p.sl:
                                p_info += f" 止损={p.sl}"
                            if p.leverage and p.leverage > 1:
                                p_info += f" 杠杆={p.leverage}x"
                            pos_lines.append(p_info)
                        _llm_context += "\n[当前持仓]\n" + "\n".join(pos_lines)
                except Exception as pos_e:
                    logger.debug(f"获取持仓上下文失败: {pos_e}")

                logger.debug(f"KOL {kol.name} 注入上下文(含原文+持仓): {_llm_context[:200]}")
        except Exception as e:
            logger.debug(f"获取历史信号上下文失败: {e}")

        # 提取 kol 属性到局部变量(避免 session 关闭后访问 ORM 对象触发懒加载)
        _kol_id = kol.id
        _kol_name = kol.name
        _kol_mc = getattr(kol, 'llm_min_confidence', None)
        _kol_min_confidence = _kol_mc if _kol_mc is not None else 0.4
    # Phase 1 结束: DB 会话已关闭

    # Phase 2: LLM 调用(不占用 DB 会话,释放连接池资源)
    # L-4修复: 解析失败时记录错误信号,不静默丢消息
    try:
        parsed = await signal_parser.parse_message(
            content,
            image_url,
            kol_config=kol_llm_config,
            kol_name=_kol_name,
            context=_llm_context,
            recent_texts=_recent_texts,
        )
    except Exception as parse_err:
        logger.exception(f"信号解析失败 kol={_kol_name} msg={message_id}: {parse_err}")
        async with AsyncSessionLocal() as db:
            try:
                err_signal = Signal(
                    kol_id=_kol_id,
                    discord_message_id=message_id,
                    raw_text=content[:2000],
                    image_url=image_url,
                    parsed={},
                    status="parse_error",
                    correct_log=f"解析异常: {str(parse_err)[:500]}",
                )
                db.add(err_signal)
                try:
                    await db.commit()
                except Exception:
                    await db.rollback()
                    logger.exception("db commit failed")
                    raise
            except Exception:
                await db.rollback()
        return

    # Phase 3: 创建信号记录并处理(使用新的 DB 会话)
    async with AsyncSessionLocal() as db:
        now = datetime.now(timezone.utc)
        signal = Signal(
            kol_id=_kol_id,
            discord_message_id=message_id,
            raw_text=content,
            image_url=image_url,
            parsed=parsed.model_dump(),
            status="received" if (
                parsed.is_exit_signal
                or parsed.is_update_signal
                or "cancel_order" in parsed.actions
                or "refresh_pending" in parsed.actions
                or (parsed.symbol and parsed.side in ("long", "short") and any(a.startswith("open_") for a in parsed.actions))
            ) else "ignored",
            received_at=now,
            symbol=parsed.symbol,
            side=parsed.side,
            entry_price=parsed.entry_price,
            confidence=parsed.confidence,
            ocr_text=parsed.ocr_text,
        )
        db.add(signal)
        # M10修复: Signal入库添加try/except,防止commit失败导致信号丢失且无日志
        try:
            await db.commit()
            await db.refresh(signal)
        except Exception as e:
            await db.rollback()
            logger.error(f"信号入库失败 kol={_kol_name} symbol={parsed.symbol}: {e}")
            return

        # 广播信号事件(管理员/前端可见)
        await bus.publish("admin", "signal", {
            "id": signal.id, "kol_id": _kol_id, "kol_name": _kol_name,
            "symbol": parsed.symbol, "side": parsed.side, "raw_text": content[:200],
            "status": signal.status, "confidence": parsed.confidence,
        })

        if signal.status == "ignored":
            _write_parser_diff_log(
                kol_id=_kol_id,
                kol_name=_kol_name,
                channel_id=channel_id,
                message_id=message_id,
                raw_text=content,
                image_url=image_url,
                signal_id=signal.id,
                parsed=parsed,
                status=signal.status,
                reason="initial_ignored_no_valid_action",
            )
            return

        # ---- 第3层过滤: 置信度阈值拦截 ----
        # 低于 KOL 配置的 min_confidence 的信号不分发给客户(借鉴 KOL 跟单系统)
        if parsed.confidence > 0 and parsed.confidence < _kol_min_confidence:
            logger.info(
                f"信号置信度 {parsed.confidence:.2f} < 阈值 {_kol_min_confidence}, 不分发: "
                f"kol={_kol_name} symbol={parsed.symbol}"
            )
            signal.status = "ignored"
            try:
                await db.commit()
            except Exception as e:
                await db.rollback()
                logger.warning(f"信号状态更新失败(ignored/置信度): {e}")
            _write_parser_diff_log(
                kol_id=_kol_id,
                kol_name=_kol_name,
                channel_id=channel_id,
                message_id=message_id,
                raw_text=content,
                image_url=image_url,
                signal_id=signal.id,
                parsed=parsed,
                status=signal.status,
                reason="below_kol_min_confidence",
                extra={"kol_min_confidence": _kol_min_confidence},
            )
            return

        # ---- 第3层过滤: action 白名单 ----
        # 只分发有效交易动作: 开仓/平仓/更新信号
        # 过滤掉 unknown/无效动作(借鉴 KOL 跟单系统 6 种 action 白名单)
        is_valid_action = (
            parsed.is_exit_signal
            or parsed.is_update_signal
            or "cancel_order" in parsed.actions
            or "refresh_pending" in parsed.actions
            or (parsed.symbol and parsed.side in ("long", "short") and any(a.startswith("open_") for a in parsed.actions))
        )
        if not is_valid_action:
            logger.info(
                f"信号无有效交易动作,不分发: kol={_kol_name} "
                f"symbol={parsed.symbol} side={parsed.side}"
            )
            signal.status = "ignored"
            try:
                await db.commit()
            except Exception as e:
                await db.rollback()
                logger.warning(f"信号状态更新失败(ignored/无效动作): {e}")
            _write_parser_diff_log(
                kol_id=_kol_id,
                kol_name=_kol_name,
                channel_id=channel_id,
                message_id=message_id,
                raw_text=content,
                image_url=image_url,
                signal_id=signal.id,
                parsed=parsed,
                status=signal.status,
                reason="invalid_action_after_parse",
            )
            return


        follows = (
            await db.execute(
                select(KolFollow).where(KolFollow.kol_id == _kol_id, KolFollow.enabled.is_(True))
            )
        ).scalars().all()
        for follow in follows:
            task = asyncio.create_task(
                _process_for_customer_sem(signal.id, follow.customer_id)
            )
            _pending_tasks.add(task)
            task.add_done_callback(lambda t, cid=follow.customer_id: (_pending_tasks.discard(t), _log_task_done(t, f"customer_{cid}")))
        _write_parser_diff_log(
            kol_id=_kol_id,
            kol_name=_kol_name,
            channel_id=channel_id,
            message_id=message_id,
            raw_text=content,
            image_url=image_url,
            signal_id=signal.id,
            parsed=parsed,
            status=signal.status,
            reason="dispatched_to_followers",
            extra={"follower_count": len(follows)},
        )


async def _handle_message_with_sem(
    payload: dict,
    discord_account_id: int | None = None,
    is_default_account: bool = False,
) -> None:
    """带并发限制的 MESSAGE_CREATE 处理包装。"""
    async with _message_semaphore:
        await _handle_message(payload, discord_account_id, is_default_account)


def _log_task_done(t: "asyncio.Task", name: str) -> None:


    """任务完成回调:记录未捕获异常,避免被 GC 静默吞掉。"""


    if t.cancelled():


        return


    exc = t.exception()


    if exc:


        logger.error(f"Discord 子任务 {name} 异常退出: {exc}")






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
        # 不清理锁,避免竞态条件;customer 数量有限,不会内存泄漏








async def _process_for_customer(signal_id: int, customer_id: int) -> None:


    """为单个客户处理信号(独立会话)。"""


    async with AsyncSessionLocal() as db:


        signal = (await db.execute(select(Signal).where(Signal.id == signal_id))).scalar_one_or_none()


        if not signal:


            return


        from app.schemas.signal import ParsedSignal





        parsed = ParsedSignal(**(signal.parsed or {}))

        customer_positions = (
            await db.execute(
                select(Position).where(
                    Position.customer_id == customer_id,
                    Position.kol_id == signal.kol_id,
                    Position.status == "open",
                    Position.parent_id.is_not(None),
                )
            )
        ).scalars().all()
        logger.info(
            f"客户实际持仓快照: customer={customer_id} signal={signal_id} "
            f"kol_id={signal.kol_id} positions={len(customer_positions)}"
        )

        await order_manager.process_signal(
            db,
            signal,
            parsed,
            customer_id,
            customer_positions=customer_positions,
        )








async def _heartbeat_with_ack(ws, interval: int, seq: list[int], last_ack: list[float]) -> None:


    """心跳循环,检测超时则关闭 WS 触发重连。"""


    while True:


        try:


            await asyncio.sleep(interval / 1000.0)


            await ws.send(json.dumps({"op": 1, "d": seq[0]}))





            # 检测心跳超时(超过 2.5 个间隔未收到 ACK)


            now = asyncio.get_event_loop().time()


            if now - last_ack[0] > (interval * 2.5 / 1000.0):


                logger.error("Discord 心跳超时,关闭连接触发重连")
                _set_source_status(connected=False, state="heartbeat_timeout", last_error="Discord 心跳超时,准备重连")


                # 主动关闭 WS,打断外层 `async for raw in ws` 的阻塞 recv,


                # 否则仅 return 会让外层继续等待,无法触发重连


                try:


                    await ws.close()


                except Exception as e:


                    logger.warning(f"Unexpected error: {e}", exc_info=True)


                return





        except Exception as e:


            logger.error(f"Discord 心跳异常: {e}")
            _set_source_status(connected=False, state="heartbeat_error", last_error=f"Discord 心跳异常: {e}")


            try:


                await ws.close()


            except Exception as e:


                logger.warning(f"Unexpected error: {e}", exc_info=True)


            return  # 退出心跳循环,触发外层重连








async def _mark_discord_account_connected(account_id: int | None) -> None:
    """记录 Discord 账号最近连接成功时间。"""
    if account_id is None:
        return
    try:
        async with AsyncSessionLocal() as db:
            acc = (await db.execute(select(DiscordAccount).where(DiscordAccount.id == account_id))).scalar_one_or_none()
            if acc:
                acc.last_connected_at = datetime.now(timezone.utc)
                acc.last_error = ""
                try:
                    await db.commit()
                except Exception:
                    await db.rollback()
                    logger.exception("db commit failed")
                    raise
    except Exception as e:
        logger.debug(f"更新 Discord 账号连接状态失败: id={account_id} err={e}")


async def _mark_discord_account_error(account_id: int | None, error: str) -> None:
    """记录 Discord 账号最近错误。"""
    if account_id is None:
        return
    try:
        async with AsyncSessionLocal() as db:
            acc = (await db.execute(select(DiscordAccount).where(DiscordAccount.id == account_id))).scalar_one_or_none()
            if acc:
                acc.last_error = error[:1000]
                try:
                    await db.commit()
                except Exception:
                    await db.rollback()
                    logger.exception("db commit failed")
                    raise
    except Exception as e:
        logger.debug(f"更新 Discord 账号错误状态失败: id={account_id} err={e}")


async def _run_single_discord_account(account) -> None:
    """单个 Discord 账号 Gateway 主循环(断线自动重连 + Token 热重载)。"""
    from app.core.runtime_config import get_discord_account_settings

    token = account.token
    token_hash = account.token_hash
    account_id = account.id
    account_label = account.label
    is_default_account = account.is_default
    reconnect_count = 0


    MAX_RECONNECT_BACKOFF = 60


    TOKEN_CHECK_INTERVAL = 60


    consecutive_failures = 0


    # RESUME 支持:保存 session_id 与 seq,断线后尝试恢复会话以补齐遗漏消息


    session_id: str | None = None


    resume_seq: int = 0





    while True:


        heartbeat_task: "asyncio.Task | None" = None


        # SC-S3 修复: watcher 也需在外层初始化,确保异常路径可清理
        watcher: "asyncio.Task | None" = None


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


                heartbeat_task = asyncio.create_task(_heartbeat_with_ack(ws, heartbeat_interval, seq, last_ack_ref))


                heartbeat_task.add_done_callback(lambda t: _log_task_done(t, "heartbeat"))





                if session_id:


                    # 尝试 RESUME:用已保存的 session_id + seq 恢复会话,补齐断线期间遗漏的消息


                    await ws.send(json.dumps({


                        "op": 6,


                        "d": {"token": token, "session_id": session_id, "seq": resume_seq},


                    }))


                    logger.info(f"Discord 尝试 RESUME: session_id={session_id} seq={resume_seq}")


                else:


                    # 首次连接或会话不可恢复 → IDENTIFY


                    await ws.send(json.dumps({


                        "op": 2,


                        "d": {


                            "token": token,


                            "intents": 512 | 32768,  # GUILD_MESSAGES | MESSAGE_CONTENT


                            "properties": {"os": "linux", "browser": "dcquant", "device": "dcquant"},


                        },


                    }))





                logger.info(


                    f"Discord Gateway 已连接,开始监听 KOL 消息 "


                    f"(累计重连 {reconnect_count} 次, 当前 session={session_id})"


                )


                reconnect_count = 0


                # SC-S1 修复: consecutive_failures 重置移至 READY/RESUMED 事件确认后




                # 并发:监听消息 + 定期检查 Token 变化


                import asyncio as _aio


                async def _watch_token_change():


                    """定期检查 Token,变了就主动断开重连。"""


                    while True:


                        await _aio.sleep(TOKEN_CHECK_INTERVAL)


                        try:


                            accounts = await get_discord_account_settings()
                            current = next(
                                (
                                    a for a in accounts
                                    if (account_id is not None and a.id == account_id)
                                    or (account_id is None and a.id is None and a.token_hash == token_hash)
                                ),
                                None,
                            )
                            if current is None or current.token_hash != token_hash or current.is_default != is_default_account:
                                logger.info(f"检测到 Discord 账号变化,触发重连: id={account_id} label={account_label}")
                                await ws.close()


                                return


                        except Exception as e:


                            logger.debug(f"Discord 账号检查异常: id={account_id} err={e}")



                watcher = _aio.create_task(_watch_token_change())





                try:


                    async for raw in ws:


                        payload = json.loads(raw)


                        op = payload.get("op")


                        if op == 0:  # Dispatch


                            seq[0] = payload.get("s", seq[0])


                            resume_seq = seq[0]


                            t = payload.get("t")


                            d = payload.get("d", {})


                            # READY 事件携带 session_id,用于后续 RESUME


                            if t == "READY":


                                session_id = d.get("session_id")


                                await _mark_discord_account_connected(account_id)
                                _set_source_status(connected=True, state="ready", session_id=session_id or "", last_connected_at=_iso_now())
                                logger.info(f"Discord READY: account={account_label}({account_id}) session_id={session_id}")
                                # SC-S1 修复: 收到 READY 确认连接成功后才重置失败计数
                                consecutive_failures = 0
                            # RESUMED 事件表示恢复成功,无需重新 IDENTIFY


                            elif t == "RESUMED":


                                logger.info("Discord RESUME 成功,已补齐遗漏消息")


                                # SC-S1 修复: RESUME 成功也重置失败计数
                                consecutive_failures = 0


                            elif t == "MESSAGE_CREATE":
                                msg_task = _aio.create_task(
                                    _handle_message_with_sem(payload, account_id, is_default_account)
                                )
                                # P2 修复: 将 msg_task 加入 _pending_tasks,防止被 GC 回收
                                _pending_tasks.add(msg_task)
                                msg_task.add_done_callback(lambda tk: (_pending_tasks.discard(tk), _log_task_done(tk, "handle_message")))


                        elif op == 11:  # Heartbeat ACK
                            last_ack_ref[0] = asyncio.get_event_loop().time()
                            _set_source_status(last_heartbeat_ack_at=_iso_now())
                        elif op == 7:  # Reconnect


                            logger.info("Discord 要求重连")


                            # SC-S2 修复: 递增失败计数并添加指数退避延迟
                            consecutive_failures += 1
                            await asyncio.sleep(min(2 ** consecutive_failures, 32))


                            break


                        elif op == 9:  # Invalid Session


                            resumable = bool(payload.get("d"))


                            # SC-S1 修复: 递增失败计数
                            consecutive_failures += 1
                            logger.warning(f"Discord Invalid Session, resumable={resumable}, consecutive_failures={consecutive_failures}")


                            # SC-S1 修复: 超过最大重连次数则记录 CRITICAL 并停止重连
                            if consecutive_failures >= MAX_RECONNECT_ATTEMPTS:
                                logger.critical(
                                    f"Discord Invalid Session 重连次数已达 {consecutive_failures} 次, "
                                    f"超过最大重连限制 {MAX_RECONNECT_ATTEMPTS}, 停止重连: "
                                    f"account={account_label}({account_id})"
                                )
                                _set_source_status(connected=False, state="fatal", last_error="重连次数超限,停止重连")
                                return


                            # 不可恢复:清除 session_id
                            if not resumable:


                                session_id = None


                            # SC-S1 修复: 使用指数退避替代固定 2 秒
                            await asyncio.sleep(min(2 ** consecutive_failures, 32))


                            await ws.close()


                            break


                finally:
                    watcher.cancel()
                    # P2 修复: cancel 后 await watcher,确保资源正确释放
                    try:
                        await watcher
                    except asyncio.CancelledError:
                        raise
                    except Exception as e:
                        logger.warning(f"Unexpected error: {e}", exc_info=True)
                    if heartbeat_task and not heartbeat_task.done():
                        heartbeat_task.cancel()
                        try:
                            await heartbeat_task
                        except asyncio.CancelledError:
                            raise





        except Exception as e:


            # SC-S3 修复: 确保异常路径下 heartbeat_task 和 watcher 被清理
            if heartbeat_task and not heartbeat_task.done():
                heartbeat_task.cancel()
                try:
                    await heartbeat_task
                except asyncio.CancelledError:
                    raise
                except Exception as e:
                    logger.warning(f"Unexpected error: {e}", exc_info=True)
            if watcher and not watcher.done():
                watcher.cancel()
                try:
                    await watcher
                except asyncio.CancelledError:
                    raise
                except Exception as e:
                    logger.warning(f"Unexpected error: {e}", exc_info=True)


            reconnect_count += 1


            consecutive_failures += 1


            backoff = min(5 * (2 ** min(reconnect_count, 4)), MAX_RECONNECT_BACKOFF)


            # 添加随机抖动(0~3秒),避免多个实例同时重连导致雪崩


            import random as _random


            jitter = _random.uniform(0, 3)


            backoff_with_jitter = backoff + jitter


            logger.exception(


                f"Discord 连接异常: account={account_label}({account_id}) err={e}, "
                f"{backoff_with_jitter:.1f} 秒后第 {reconnect_count} 次重连"
            )


            _set_source_status(connected=False, state="reconnecting", last_error=str(e), reconnect_count=reconnect_count, consecutive_failures=consecutive_failures)
            await _mark_discord_account_error(account_id, str(e))
            await asyncio.sleep(backoff_with_jitter)





        # 重连前重新读取该账号 Token(可能已更新)
        try:


            accounts = await get_discord_account_settings()
            current = next(
                (
                    a for a in accounts
                    if (account_id is not None and a.id == account_id)
                    or (account_id is None and a.id is None and a.token_hash == token_hash)
                ),
                None,
            )
            if current is None:
                logger.info(f"Discord 账号已禁用或删除,停止监听: id={account_id} label={account_label}")
                return
            if current.token_hash != token_hash:
                token = current.token
                token_hash = current.token_hash
                is_default_account = current.is_default
                account_label = current.label
                logger.info(f"Discord Token 已更新,使用新 Token 重连: id={account_id} label={account_label}")
        except Exception as e:


            logger.warning(f"Unexpected error: {e}", exc_info=True)


def _discord_account_task_key(account) -> str:
    """生成运行中账号任务的稳定 key。"""
    return str(account.id) if account.id is not None else f"legacy:{account.token_hash}"


async def run_discord_monitor() -> None:
    """Discord 多账号监听编排循环。

    每个启用的 DiscordAccount 独立建立 Gateway 连接;默认账号同时兼容未绑定账号的旧 KOL。
    """
    from app.core.runtime_config import get_discord_account_settings

    CHECK_INTERVAL = 60
    tasks: dict[str, asyncio.Task] = {}

    try:
        while True:
            accounts = await get_discord_account_settings()
            _set_source_status(
                configured=bool(accounts),
                state="starting" if accounts else "not_configured",
                connected=False if not accounts else _SOURCE_STATUS.get("connected", False),
            )
            active_keys = {_discord_account_task_key(a) for a in accounts if a.token}

            if not accounts:
                if not tasks:
                    logger.warning("未配置 Discord Token,Discord 监听等待配置后启动")
            else:
                for account in accounts:
                    if not account.token:
                        continue
                    key = _discord_account_task_key(account)
                    task = tasks.get(key)
                    if task and not task.done():
                        continue
                    if task and task.done():
                        _log_task_done(task, f"discord_account_{key}")
                    task = asyncio.create_task(_run_single_discord_account(account))
                    tasks[key] = task
                    logger.info(
                        f"已启动 Discord 账号监听: id={account.id} "
                        f"label={account.label} default={account.is_default}"
                    )

            for key in list(tasks.keys()):
                if key not in active_keys:
                    task = tasks.pop(key)
                    task.cancel()
                    logger.info(f"已停止 Discord 账号监听: key={key}")
                elif tasks[key].done():
                    _log_task_done(tasks[key], f"discord_account_{key}")
                    tasks.pop(key, None)

            await asyncio.sleep(CHECK_INTERVAL)
    finally:
        for task in tasks.values():
            task.cancel()
        await asyncio.gather(*tasks.values(), return_exceptions=True)
