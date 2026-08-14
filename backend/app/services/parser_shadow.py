"""影子解析旁路。

本模块只做新旧解析对比和记录，不参与真实下单决策。
"""
from __future__ import annotations

from datetime import datetime
from typing import Any

from loguru import logger
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.signal import ParserShadowResult, Signal
from app.schemas.signal import ParsedSignal
from app.services import signal_parser

SHADOW_PARSE_VERSION = "shadow_rule_context_v1"


def _safe_dump(parsed: ParsedSignal | dict | None) -> dict[str, Any]:
    if parsed is None:
        return {}
    if isinstance(parsed, dict):
        return dict(parsed)
    try:
        return parsed.model_dump()
    except Exception:
        return {}


def _round_value(value: Any) -> Any:
    if isinstance(value, float):
        return round(value, 8)
    if isinstance(value, list):
        return [_round_value(v) for v in value]
    if isinstance(value, dict):
        return {k: _round_value(v) for k, v in value.items()}
    return value


def _core_value(data: dict[str, Any], field: str) -> Any:
    value = data.get(field)
    if value is None:
        return None
    if isinstance(value, str):
        return value.strip()
    return _round_value(value)


def _status_from_parsed(parsed: ParsedSignal) -> str:
    actions = parsed.actions or []
    valid = (
        parsed.is_exit_signal
        or parsed.is_update_signal
        or "cancel_order" in actions
        or "refresh_pending" in actions
        or (
            parsed.symbol
            and parsed.side in ("long", "short")
            and any(a.startswith("open_") for a in actions)
        )
    )
    return "received" if valid else "ignored"


def _compare(old_data: dict[str, Any], new_data: dict[str, Any]) -> tuple[dict[str, Any], list[str]]:
    fields = [
        "action",
        "actions",
        "symbol",
        "side",
        "entry_price",
        "entry_prices",
        "take_profits",
        "stop_loss",
        "condition_price",
        "leverage",
        "position_pct",
        "is_exit_signal",
        "is_update_signal",
        "reason",
    ]
    diff: dict[str, Any] = {}
    mismatch_fields: list[str] = []
    for field in fields:
        old_value = _core_value(old_data, field)
        new_value = _core_value(new_data, field)
        if old_value != new_value:
            mismatch_fields.append(field)
            diff[field] = {"old": old_value, "new": new_value}
    return diff, mismatch_fields


def _shadow_parse(raw_text: str, recent_texts: list[str] | None = None) -> ParsedSignal:
    parsed = signal_parser.parse_text(raw_text or "")
    try:
        parsed = signal_parser.apply_position_context_if_needed(raw_text or "", parsed, recent_texts or [])
    except Exception as e:
        logger.debug(f"影子解析上下文补全失败: {e}")
    try:
        intent, intent_reason = signal_parser.classify_signal_intent(raw_text or "")
        scene, scene_reason = signal_parser.classify_signal_scene(raw_text or "")
        parsed.reason = parsed.reason or f"intent={intent}:{intent_reason}; scene={scene}:{scene_reason}"
    except Exception:
        pass
    return parsed


async def record_shadow_parse(
    db: AsyncSession,
    signal: Signal,
    live_parsed: ParsedSignal,
    *,
    kol_id: int,
    discord_message_id: str,
    raw_text: str,
    image_url: str = "",
    source: str = "discord",
    recent_texts: list[str] | None = None,
    signal_received_at: datetime | None = None,
) -> None:
    """执行影子解析并写入对比结果。

    失败会被吞掉并回滚当前影子写入，不能影响真实信号分发和下单。
    """
    try:
        shadow_parsed = _shadow_parse(raw_text, recent_texts=recent_texts)
        old_data = _safe_dump(live_parsed)
        new_data = _safe_dump(shadow_parsed)
        diff, mismatch_fields = _compare(old_data, new_data)
        old_status = signal.status or _status_from_parsed(live_parsed)
        new_status = _status_from_parsed(shadow_parsed)

        row = ParserShadowResult(
            signal_id=signal.id,
            kol_id=kol_id,
            discord_message_id=discord_message_id or "",
            raw_text=raw_text or "",
            image_url=image_url or "",
            source=source,
            parse_version=SHADOW_PARSE_VERSION,
            old_parsed=old_data,
            new_parsed=new_data,
            diff=diff,
            mismatch_fields=mismatch_fields,
            old_status=old_status,
            new_status=new_status,
            old_symbol=old_data.get("symbol") or "",
            new_symbol=new_data.get("symbol") or "",
            old_side=old_data.get("side") or "",
            new_side=new_data.get("side") or "",
            old_entry_price=old_data.get("entry_price"),
            new_entry_price=new_data.get("entry_price"),
            old_stop_loss=old_data.get("stop_loss"),
            new_stop_loss=new_data.get("stop_loss"),
            status="pending" if mismatch_fields else "ignored",
            signal_received_at=signal_received_at or signal.received_at,
        )
        db.add(row)
        try:
            await db.commit()
        except Exception:
            await db.rollback()
            logger.exception("db commit failed")
            raise
        logger.info(
            f"影子解析已记录: signal_id={signal.id} kol_id={kol_id} "
            f"diff_fields={mismatch_fields}"
        )
    except Exception as e:
        await db.rollback()
        logger.warning(f"影子解析记录失败但不影响真实链路: signal_id={getattr(signal, 'id', None)} err={e}")
