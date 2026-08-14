"""设置路由:交易所账号(加密导入)、风控(静默时段)、飞书告警。"""
import hashlib
from datetime import datetime, timezone

from fastapi import APIRouter, Depends, HTTPException
from loguru import logger
from sqlalchemy import func, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.database import get_db
from app.core.security import decrypt_secret, encrypt_secret, get_current_user, require_customer
from app.models.audit import AlertLog, AuditLog
from app.models.config import AlertConfig, ExchangeAccount, RiskConfig
from app.models.customer import Customer
from app.models.trading import Order, Position
from app.schemas.common import ok
from app.schemas.config import (
    AlertConfigCreate,
    AlertConfigOut,
    ExchangeAccountCreate,
    ExchangeAccountFollowUpdate,
    ExchangeAccountOut,
    RiskConfigOut,
    RiskConfigUpdate,
)

router = APIRouter(tags=["设置"])

# ★ P2 修复: AlertConfig 显式允许字段白名单,防止 Mass Assignment
ALERT_UPDATABLE_FIELDS = {
    "name", "webhook_url", "webhook_secret", "enabled",
    "on_signal", "on_order", "on_tp_sl", "on_correct",
    "on_risk", "on_auth_expire", "on_error",
}


def _mask(key: str, show: int = 4) -> str:
    """掩码 API Key。show 参数控制显示首尾各几位,默认 4。"""
    if not key or len(key) <= show * 2:
        return "***"
    return f"{key[:show]}...{key[-show:]}"


def _hash_api_key(api_key: str) -> str:
    """API Key 的 SHA256 哈希,用于跨客户唯一性校验。"""
    return hashlib.sha256(api_key.encode("utf-8")).hexdigest()


def _api_status(acc: ExchangeAccount) -> str:
    """返回 API 三态:unverified/verified/failed。"""
    if acc.last_error:
        return "failed"
    if getattr(acc, "last_verified_at", None):
        return "verified"
    return "unverified"


def _account_mode(exchange: str, testnet: bool, account_mode: str | None = None) -> str:
    """统一交易所环境标识,兼容旧 testnet 布尔字段。"""
    exchange_name = (exchange or "").strip().lower()
    mode = (account_mode or "").strip().lower()
    if not mode:
        return "testnet" if testnet else "live"
    if mode not in {"live", "testnet", "demo"}:
        raise HTTPException(400, "交易环境无效")
    if mode == "demo" and exchange_name not in {"bybit", "binance"}:
        raise HTTPException(400, "当前只有 Bybit/Binance 支持 Demo Trading")
    return mode


async def _audit(db: AsyncSession, user_id: int, action: str, target: str, detail: str) -> None:
    """记录客户侧设置操作审计。AuditLog.user_id 关联管理员用户表,客户操作不写 user_id。"""
    db.add(AuditLog(action=action, target=target[:128], detail=detail[:2000], ip=""))


# ---------- 交易所账号 ----------
@router.get("/exchange-accounts")
async def list_exchange_accounts(current=Depends(get_current_user), db: AsyncSession = Depends(get_db)):
    cid = current.id if current.role == "customer" else None
    if cid is None:
        # admin: return all active accounts
        rows = (
            await db.execute(
                select(ExchangeAccount)
                .where(ExchangeAccount.is_active.is_(True))
                .order_by(ExchangeAccount.customer_id, ExchangeAccount.exchange, ExchangeAccount.account_mode, ExchangeAccount.is_default.desc(), ExchangeAccount.id)
            )
        ).scalars().all()
    else:
        rows = (
            await db.execute(
                select(ExchangeAccount)
                .where(ExchangeAccount.customer_id == cid, ExchangeAccount.is_active.is_(True))
                .order_by(ExchangeAccount.exchange, ExchangeAccount.account_mode, ExchangeAccount.is_default.desc(), ExchangeAccount.id)
            )
        ).scalars().all()
    out = []
    for r in rows:
        d = ExchangeAccountOut.model_validate(r).model_dump()
        # Safeguard: ensure encrypted secrets are never exposed in responses
        for _secret_field in ("api_key_enc", "api_secret_enc", "passphrase_enc", "api_key_hash"):
            d.pop(_secret_field, None)
        try:
            # M-2修复: 管理员视图掩码更严格(仅显示后2位),客户视图保持前4后4
            if current.role == "admin":
                d["api_key_mask"] = _mask(decrypt_secret(r.api_key_enc), show=2)
            else:
                d["api_key_mask"] = _mask(decrypt_secret(r.api_key_enc))
        except Exception:
            d["api_key_mask"] = "***"
        d["status"] = _api_status(r)
        out.append(d)
    return ok(out)


@router.post("/exchange-accounts")
async def add_exchange_account(
    body: ExchangeAccountCreate, current=Depends(require_customer), db: AsyncSession = Depends(get_db)
):
    """绑定交易所 API。

    防共用三层校验:
      1. 同客户同交易所已有 API → 拒绝(每客户每交易所只能 1 个 API)
      2. 未授权多开 + 已绑其他交易所 → 拒绝(默认只能绑 1 个交易所)
      3. API Key 哈希已被其他客户绑定 → 拒绝(防止跨客户共用同一 API Key)
    """
    # 取客户信息
    cust = (
        await db.execute(select(Customer).where(Customer.id == current.id))
    ).scalar_one_or_none()
    if not cust:
        raise HTTPException(404, "客户不存在")

    # 查该客户已有的活跃 API
    existing = (
        await db.execute(
            select(ExchangeAccount).where(
                ExchangeAccount.customer_id == current.id,
                ExchangeAccount.is_active.is_(True),
            )
        )
    ).scalars().all()

    body_mode = _account_mode(body.exchange, body.testnet, body.account_mode)
    body_testnet = body_mode != "live"

    # 校验 1: 同客户同交易所同环境 API 数量限制。
    # 默认最多 1 个；开启“单交易所多 API”或“多交易所”后,按管理员配置的数量限制。
    same_exchange_mode_count = 0
    for acc in existing:
        acc_mode = _account_mode(acc.exchange, acc.testnet, getattr(acc, "account_mode", None))
        if acc.exchange == body.exchange and acc_mode == body_mode:
            same_exchange_mode_count += 1
    same_exchange_limit = 1
    if cust.single_exchange_multi_api_allowed or cust.multi_exchange_allowed:
        same_exchange_limit = max(1, int(getattr(cust, "single_exchange_multi_api_limit", 2) or 2))
    if same_exchange_mode_count >= same_exchange_limit:
        if same_exchange_limit <= 1:
            raise HTTPException(400, "same exchange/mode already has an API; enable single-exchange multi-API in admin first")
        raise HTTPException(400, f"同一交易所/账户模式最多允许绑定 {same_exchange_limit} 个 API,请联系管理员调整允许数量")

    # Rule 2: without multi-exchange permission, only the already-bound exchange can be used.
    if existing and not cust.multi_exchange_allowed:
        bound_exchanges = sorted({a.exchange for a in existing})
        if body.exchange not in bound_exchanges:
            raise HTTPException(
                400,
                f"default account can bind only 1 exchange (bound:{','.join(bound_exchanges)}); enable multi-exchange in admin first",
            )

    api_key_hash = _hash_api_key(body.api_key)
    dup = (
        await db.execute(
            select(ExchangeAccount).where(
                ExchangeAccount.api_key_hash == api_key_hash,
                ExchangeAccount.is_active.is_(True),
            )
        )
    ).scalar_one_or_none()
    if dup and dup.customer_id != current.id:
        raise HTTPException(400, "该 API Key 已被其他账号绑定,禁止共用交易所 API")

    if body.strategy_id:
        from app.models.strategy import Strategy

        strat = (
            await db.execute(
                select(Strategy).where(
                    Strategy.id == body.strategy_id,
                    Strategy.customer_id == current.id,
                    Strategy.enabled.is_(True),
                )
            )
        ).scalar_one_or_none()
        if not strat:
            raise HTTPException(400, "策略不存在或已停用")

    has_default = any(a.is_default for a in existing)

    acc = ExchangeAccount(
        customer_id=current.id,
        exchange=body.exchange,
        label=body.label,
        api_key_enc=encrypt_secret(body.api_key),
        api_secret_enc=encrypt_secret(body.api_secret),
        passphrase_enc=encrypt_secret(body.passphrase) if body.passphrase else "",
        api_key_hash=api_key_hash,
        testnet=body_testnet,
        account_mode=body_mode,
        is_active=True,
        follow_enabled=body.follow_enabled,
        follow_weight=body.follow_weight,
        max_order_usdt=body.max_order_usdt,
        strategy_id=body.strategy_id,
        # 多 API 场景下,"默认下单 API" 是客户级唯一入口。
        # 新增账号不会抢占已有默认账号；只有客户没有任何默认账号时才自动设为默认。
        is_default=not has_default,
    )
    db.add(acc)
    await _audit(db, current.id, "exchange_account_create", f"exchange_account:{current.id}:{body.exchange}", f"customer_id={current.id}, exchange={body.exchange}, account_mode={body_mode}")
    try:
        await db.commit()
    except IntegrityError:
        await db.rollback()
        raise HTTPException(400, "该API Key已存在,不能重复添加")
    except Exception:
        await db.rollback()
        logger.exception("绑定交易所API失败")
        raise HTTPException(500, "绑定交易所API失败,请稍后重试")
    out = ExchangeAccountOut.model_validate(acc).model_dump()
    # Safeguard: ensure encrypted secrets are never exposed in responses
    for _secret_field in ("api_key_enc", "api_secret_enc", "passphrase_enc", "api_key_hash"):
        out.pop(_secret_field, None)
    return ok(out)


@router.post("/exchange-accounts/{aid}/default")
async def set_default_exchange_account(aid: int, customer_id: int | None = None, current=Depends(get_current_user), db: AsyncSession = Depends(get_db)):
    """设置客户级默认下单 API。

    默认 API 用于手动下单和未显式开启多 API 跟单时的兼容兜底；
    自动跟单会优先使用所有 follow_enabled=True 且验证正常的 API。
    """
    # M-1修复: 管理员操作时必须指定 customer_id,审计日志记录目标客户 ID
    if current.role == "admin":
        if customer_id is None:
            raise HTTPException(400, "管理员操作需要指定 customer_id")
        cid = customer_id
    else:
        cid = current.id
    q = select(ExchangeAccount).where(
        ExchangeAccount.id == aid,
        ExchangeAccount.is_active.is_(True),
        ExchangeAccount.customer_id == cid,
    )
    acc = (await db.execute(q)).scalar_one_or_none()
    if not acc:
        raise HTTPException(404, "账号不存在或已禁用")
    if acc.last_error:
        raise HTTPException(400, "该 API 最近验证失败,请先测试连接成功后再设为默认")

    rows = (
        await db.execute(
            select(ExchangeAccount).where(
                ExchangeAccount.customer_id == cid,
                ExchangeAccount.is_active.is_(True),
            )
        )
    ).scalars().all()
    for row in rows:
        row.is_default = row.id == aid
    await _audit(db, current.id, "exchange_account_set_default", f"exchange_account:{aid}", f"customer_id={cid}, exchange={acc.exchange}, testnet={acc.testnet}")
    try:
        await db.commit()
    except Exception:
        await db.rollback()
        logger.exception("设置默认交易所账号失败")
        raise HTTPException(500, "设置默认账号失败,请稍后重试")
    return ok({"id": aid, "exchange": acc.exchange, "testnet": acc.testnet, "is_default": True})


@router.put("/exchange-accounts/{aid}/follow")
async def update_exchange_account_follow(
    aid: int,
    body: ExchangeAccountFollowUpdate,
    current=Depends(require_customer),
    db: AsyncSession = Depends(get_db),
):
    """更新单个 API 的自动跟单配置。

    多 API 跟单第一版约定:
      - follow_enabled=True 的 API 会参与自动跟单;
      - follow_weight 作为该 API 独立下单倍率;
      - max_order_usdt=0 表示不限,否则对该 API 单笔金额封顶;
      - strategy_id 为空时沿用 KOL 跟随策略。
    """
    acc = (
        await db.execute(
            select(ExchangeAccount).where(
                ExchangeAccount.id == aid,
                ExchangeAccount.customer_id == current.id,
                ExchangeAccount.is_active.is_(True),
            )
        )
    ).scalar_one_or_none()
    if not acc:
        raise HTTPException(404, "账号不存在或已禁用")

    data = body.model_dump(exclude_unset=True)
    if "strategy_id" in data and data["strategy_id"]:
        from app.models.strategy import Strategy

        strat = (
            await db.execute(
                select(Strategy).where(
                    Strategy.id == data["strategy_id"],
                    Strategy.customer_id == current.id,
                    Strategy.enabled.is_(True),
                )
            )
        ).scalar_one_or_none()
        if not strat:
            raise HTTPException(400, "策略不存在或已停用")

    for field in ("follow_enabled", "follow_weight", "max_order_usdt", "strategy_id"):
        if field in data:
            setattr(acc, field, data[field])

    await _audit(
        db,
        current.id,
        "exchange_account_follow_update",
        f"exchange_account:{aid}",
        (
            f"customer_id={current.id}, exchange={acc.exchange}, testnet={acc.testnet}, "
            f"follow_enabled={acc.follow_enabled}, follow_weight={acc.follow_weight}, "
            f"max_order_usdt={acc.max_order_usdt}, strategy_id={acc.strategy_id}"
        ),
    )
    try:
        await db.commit()
    except Exception:
        await db.rollback()
        logger.exception("更新 API 跟单配置失败")
        raise HTTPException(500, "更新 API 跟单配置失败,请稍后重试")
    await db.refresh(acc)
    out = ExchangeAccountOut.model_validate(acc).model_dump()
    try:
        out["api_key_mask"] = _mask(decrypt_secret(acc.api_key_enc))
    except Exception:
        out["api_key_mask"] = "***"
    out["status"] = _api_status(acc)
    return ok(out)


@router.delete("/exchange-accounts/{aid}")
async def delete_exchange_account(aid: int, current=Depends(get_current_user), db: AsyncSession = Depends(get_db)):
    acc = (await db.execute(select(ExchangeAccount).where(ExchangeAccount.id == aid, ExchangeAccount.customer_id == current.id))).scalar_one_or_none()
    if not acc:
        raise HTTPException(404, "账号不存在")
    was_default = bool(acc.is_default)
    exchange = acc.exchange
    testnet = acc.testnet
    replacement_id = None
    acc.is_active = False
    acc.is_default = False
    if was_default:
        replacement = (
            await db.execute(
                select(ExchangeAccount)
                .where(
                    ExchangeAccount.customer_id == current.id,
                    ExchangeAccount.is_active.is_(True),
                    ExchangeAccount.id != aid,
                    ExchangeAccount.last_error.is_(None) | (ExchangeAccount.last_error == ""),
                )
                .order_by(ExchangeAccount.last_verified_at.desc().nullslast(), ExchangeAccount.id)
            )
        ).scalars().first()
        if not replacement:
            replacement = (
                await db.execute(
                    select(ExchangeAccount)
                    .where(
                        ExchangeAccount.customer_id == current.id,
                        ExchangeAccount.is_active.is_(True),
                        ExchangeAccount.id != aid,
                    )
                    .order_by(ExchangeAccount.last_error.asc(), ExchangeAccount.id)
                )
            ).scalars().first()
        if replacement:
            replacement.is_default = True
            replacement_id = replacement.id
    await _audit(db, current.id, "exchange_account_delete", f"exchange_account:{aid}", f"customer_id={current.id}, exchange={exchange}, testnet={testnet}, was_default={was_default}, replacement_id={replacement_id}")
    try:
        await db.commit()
    except Exception:
        await db.rollback()
        logger.exception("删除交易所账号失败")
        raise HTTPException(500, "删除交易所账号失败,请稍后重试")
    return ok({"id": aid, "is_active": False, "replacement_default_id": replacement_id})


@router.post("/exchange-accounts/{aid}/test")
async def test_exchange_account(aid: int, current=Depends(get_current_user), db: AsyncSession = Depends(get_db)):
    from app.services import exchange_adapter

    acc = (await db.execute(select(ExchangeAccount).where(ExchangeAccount.id == aid, ExchangeAccount.customer_id == current.id, ExchangeAccount.is_active.is_(True)))).scalar_one_or_none()
    if not acc:
        raise HTTPException(404, "账号不存在或已禁用")

    ex = None
    try:
        api_key = decrypt_secret(acc.api_key_enc)
        api_secret = decrypt_secret(acc.api_secret_enc)
        passphrase = decrypt_secret(acc.passphrase_enc) if acc.passphrase_enc else ""
        ex = exchange_adapter._create_exchange(acc.exchange, api_key, api_secret, passphrase, acc.testnet, getattr(acc, "account_mode", None))
        await ex.load_markets()
        bal = await exchange_adapter.fetch_balance(ex)
        acc.last_error = ""
        acc.last_verified_at = datetime.now(timezone.utc)
        await _audit(db, current.id, "exchange_account_test_success", f"exchange_account:{aid}", f"customer_id={current.id}, exchange={acc.exchange}, testnet={acc.testnet}, equity={bal.get('equity', 0)}")
        try:
            await db.commit()
        except Exception:
            await db.rollback()
            logger.exception("db commit failed")
            raise HTTPException(500, "操作失败,请稍后重试")
        return ok({
            "success": True,
            "exchange": acc.exchange,
            "account_mode": getattr(acc, "account_mode", "testnet" if acc.testnet else "live"),
            "equity": bal.get("equity", 0),
            "balance": bal.get("balance", 0),
            "last_verified_at": acc.last_verified_at.isoformat(),
            "message": f"连接成功,权益 {bal.get('equity', 0):.2f} USDT",
        })
    except Exception as e:
        acc.last_error = str(e)[:500]
        await _audit(db, current.id, "exchange_account_test_failed", f"exchange_account:{aid}", f"customer_id={current.id}, exchange={acc.exchange}, testnet={acc.testnet}, error={acc.last_error}")
        try:
            await db.commit()
        except Exception:
            await db.rollback()
            logger.exception("保存连接测试失败记录失败")
        logger.exception("交易所连接测试失败")
        raise HTTPException(400, "连接失败,请检查配置后重试")
    finally:
        if ex:
            await exchange_adapter.close_exchange(ex)


@router.get("/exchange-accounts/{aid}/balance")
async def get_exchange_account_balance(aid: int, current=Depends(require_customer), db: AsyncSession = Depends(get_db)):
    """手动刷新单个 API 的账户余额。不会自动轮询,避免增加交易所 API 压力。"""
    from app.services import exchange_adapter

    acc = (await db.execute(select(ExchangeAccount).where(ExchangeAccount.id == aid, ExchangeAccount.customer_id == current.id, ExchangeAccount.is_active.is_(True)))).scalar_one_or_none()
    if not acc:
        raise HTTPException(404, "账号不存在或已禁用")

    ex = None
    try:
        api_key = decrypt_secret(acc.api_key_enc)
        api_secret = decrypt_secret(acc.api_secret_enc)
        passphrase = decrypt_secret(acc.passphrase_enc) if acc.passphrase_enc else ""
        ex = exchange_adapter._create_exchange(acc.exchange, api_key, api_secret, passphrase, acc.testnet, getattr(acc, "account_mode", None))
        await ex.load_markets()
        bal = await exchange_adapter.fetch_balance(ex)
        acc.last_error = ""
        acc.last_verified_at = datetime.now(timezone.utc)
        await _audit(db, current.id, "exchange_account_balance_refresh", f"exchange_account:{aid}", f"customer_id={current.id}, exchange={acc.exchange}, testnet={acc.testnet}, equity={bal.get('equity', 0)}")
        try:
            await db.commit()
        except Exception:
            await db.rollback()
            logger.exception("db commit failed")
            raise HTTPException(500, "操作失败,请稍后重试")
        return ok({
            "id": acc.id,
            "exchange": acc.exchange,
            "testnet": acc.testnet,
            "account_mode": getattr(acc, "account_mode", "testnet" if acc.testnet else "live"),
            "equity": bal.get("equity", 0),
            "balance": bal.get("balance", 0),
            "unrealized_pnl": bal.get("unrealized_pnl", 0),
            "last_verified_at": acc.last_verified_at.isoformat(),
            "refreshed_at": acc.last_verified_at.isoformat(),
        })
    except Exception as e:
        acc.last_error = str(e)[:500]
        await _audit(db, current.id, "exchange_account_balance_failed", f"exchange_account:{aid}", f"customer_id={current.id}, exchange={acc.exchange}, testnet={acc.testnet}, error={acc.last_error}")
        try:
            await db.commit()
        except Exception:
            await db.rollback()
            logger.exception("保存余额刷新失败记录失败")
        logger.exception("交易所余额刷新失败")
        raise HTTPException(400, f"余额刷新失败:{acc.last_error}")
    finally:
        if ex:
            await exchange_adapter.close_exchange(ex)


@router.get("/exchange-account-balances/summary")
async def get_exchange_balance_summary(current=Depends(require_customer), db: AsyncSession = Depends(get_db)):
    """手动刷新当前客户全部活跃 API 余额,并返回交易所维度汇总。"""
    from app.services import exchange_adapter

    rows = (
        await db.execute(
            select(ExchangeAccount)
            .where(ExchangeAccount.customer_id == current.id, ExchangeAccount.is_active.is_(True))
            .order_by(ExchangeAccount.exchange, ExchangeAccount.account_mode, ExchangeAccount.is_default.desc(), ExchangeAccount.id)
        )
    ).scalars().all()

    accounts = []
    totals: dict[str, dict] = {}
    total_equity = 0.0
    total_balance = 0.0
    refreshed_at = datetime.now(timezone.utc)

    for acc in rows:
        item = {
            "id": acc.id,
            "exchange": acc.exchange,
            "testnet": acc.testnet,
            "account_mode": getattr(acc, "account_mode", "testnet" if acc.testnet else "live"),
            "is_default": acc.is_default,
            "status": _api_status(acc),
            "equity": 0.0,
            "balance": 0.0,
            "unrealized_pnl": 0.0,
            "last_verified_at": acc.last_verified_at.isoformat() if acc.last_verified_at else None,
            "refreshed_at": None,
            "error": "",
        }
        ex = None
        try:
            api_key = decrypt_secret(acc.api_key_enc)
            api_secret = decrypt_secret(acc.api_secret_enc)
            passphrase = decrypt_secret(acc.passphrase_enc) if acc.passphrase_enc else ""
            ex = exchange_adapter._create_exchange(acc.exchange, api_key, api_secret, passphrase, acc.testnet, getattr(acc, "account_mode", None))
            await ex.load_markets()
            bal = await exchange_adapter.fetch_balance(ex)
            equity = float(bal.get("equity", 0) or 0)
            balance = float(bal.get("balance", 0) or 0)
            unrealized_pnl = float(bal.get("unrealized_pnl", 0) or 0)
            acc.last_error = ""
            acc.last_verified_at = refreshed_at
            item.update({
                "status": "verified",
                "equity": equity,
                "balance": balance,
                "unrealized_pnl": unrealized_pnl,
                "last_verified_at": refreshed_at.isoformat(),
                "refreshed_at": refreshed_at.isoformat(),
            })
            total_equity += equity
            total_balance += balance
            mode = getattr(acc, "account_mode", "testnet" if acc.testnet else "live")
            key = f"{acc.exchange}-{mode}"
            group = totals.setdefault(key, {"exchange": acc.exchange, "testnet": acc.testnet, "account_mode": mode, "equity": 0.0, "balance": 0.0, "unrealized_pnl": 0.0, "account_count": 0, "failed_count": 0})
            group["equity"] += equity
            group["balance"] += balance
            group["unrealized_pnl"] += unrealized_pnl
            group["account_count"] += 1
        except Exception as e:
            acc.last_error = str(e)[:500]
            item["status"] = "failed"
            item["error"] = acc.last_error
            mode = getattr(acc, "account_mode", "testnet" if acc.testnet else "live")
            key = f"{acc.exchange}-{mode}"
            group = totals.setdefault(key, {"exchange": acc.exchange, "testnet": acc.testnet, "account_mode": mode, "equity": 0.0, "balance": 0.0, "unrealized_pnl": 0.0, "account_count": 0, "failed_count": 0})
            group["account_count"] += 1
            group["failed_count"] += 1
        finally:
            if ex:
                await exchange_adapter.close_exchange(ex)
        accounts.append(item)

    await _audit(db, current.id, "exchange_balance_summary_refresh", f"customer:{current.id}", f"customer_id={current.id}, accounts={len(accounts)}, total_equity={total_equity}")
    try:
        await db.commit()
    except Exception:
        await db.rollback()
        logger.exception("保存余额刷新审计失败")
    return ok({
        "total_equity": total_equity,
        "total_balance": total_balance,
        "refreshed_at": refreshed_at.isoformat(),
        "accounts": accounts,
        "groups": list(totals.values()),
    })


@router.get("/exchange-account-risk-overview")
async def get_exchange_risk_overview(current=Depends(require_customer), db: AsyncSession = Depends(get_db)):
    """交易设置页风险概览:账户状态、持仓/挂单、风控限制。"""
    accounts = (
        await db.execute(
            select(ExchangeAccount)
            .where(ExchangeAccount.customer_id == current.id, ExchangeAccount.is_active.is_(True))
            .order_by(ExchangeAccount.exchange, ExchangeAccount.account_mode, ExchangeAccount.is_default.desc(), ExchangeAccount.id)
        )
    ).scalars().all()
    risk_rows = (await db.execute(select(RiskConfig).where(RiskConfig.customer_id == current.id, RiskConfig.enabled.is_(True)))).scalars().all()
    risk_map = {r.exchange: r for r in risk_rows}

    out = []
    seen: set[tuple[str, str]] = set()
    for acc in accounts:
        mode = getattr(acc, "account_mode", "testnet" if acc.testnet else "live")
        key = (acc.exchange, mode)
        if key in seen:
            continue
        seen.add(key)
        cfg = risk_map.get(acc.exchange) or risk_map.get("all")
        position_count = (
            await db.execute(
                select(func.count(Position.id)).where(
                    Position.customer_id == current.id,
                    Position.exchange == acc.exchange,
                    Position.status == "open",
                )
            )
        ).scalar_one()
        pending_count = (
            await db.execute(
                select(func.count(Order.id)).where(
                    Order.customer_id == current.id,
                    Order.exchange == acc.exchange,
                    Order.status == "pending",
                )
            )
        ).scalar_one()
        max_concurrent = cfg.max_concurrent_positions if cfg else 0
        remaining_slots = None if not max_concurrent else max(0, max_concurrent - position_count)
        out.append({
            "exchange": acc.exchange,
            "testnet": acc.testnet,
            "account_mode": mode,
            "api_status": _api_status(acc),
            "default_account_id": acc.id if acc.is_default else None,
            "open_positions": position_count,
            "pending_orders": pending_count,
            "max_position_usdt": cfg.max_position_usdt if cfg else 0,
            "max_concurrent_positions": max_concurrent,
            "remaining_position_slots": remaining_slots,
            "max_daily_loss_pct": cfg.max_daily_loss_pct if cfg else 0,
            "can_open_more": remaining_slots is None or remaining_slots > 0,
        })
    return ok(out)


# ---------- 风控配置 ----------
@router.get("/risk-config")
async def get_risk_config(current=Depends(require_customer), db: AsyncSession = Depends(get_db)):
    cfg = (await db.execute(select(RiskConfig).where(RiskConfig.customer_id == current.id))).scalars().all()
    if not cfg:
        return ok(None)
    return ok([RiskConfigOut.model_validate(c).model_dump() for c in cfg])


@router.put("/risk-config")
async def upsert_risk_config(
    body: RiskConfigUpdate, current=Depends(require_customer), db: AsyncSession = Depends(get_db)
):
    # ★ P2 修复: 验证 stop_loss 百分比(default_sl_pct)在 -0.5 到 0 之间
    if hasattr(body, 'default_sl_pct') and body.default_sl_pct is not None:
        if not (-0.5 <= body.default_sl_pct <= 0):
            raise HTTPException(400, f"止损百分比必须在 -0.5 到 0 之间(当前: {body.default_sl_pct})")
    # ★ P2 修复: 验证 auto_stop_loss_pct 在 0~100 之间(0=禁用,百分比)
    if hasattr(body, 'auto_stop_loss_pct') and body.auto_stop_loss_pct is not None:
        if not (0 <= body.auto_stop_loss_pct <= 100):
            raise HTTPException(400, "止损比例需在0~100%之间")

    existing = (
        await db.execute(select(RiskConfig).where(RiskConfig.customer_id == current.id, RiskConfig.exchange == body.exchange))
    ).scalar_one_or_none()
    if existing:
        for k, v in body.model_dump(exclude={"customer_id", "id"}).items():
            setattr(existing, k, v)
        cfg = existing
    else:
        cfg = RiskConfig(customer_id=current.id, **body.model_dump(exclude={"customer_id", "id"}))
        db.add(cfg)
    try:
        await db.commit()
    except Exception:
        await db.rollback()
        logger.exception("更新风控配置失败")
        raise HTTPException(500, "更新风控配置失败,请稍后重试")
    return ok(RiskConfigOut.model_validate(cfg).model_dump())


# ---------- 飞书告警 ----------
@router.get("/alerts")
async def list_alerts(current=Depends(get_current_user), db: AsyncSession = Depends(get_db)):
    cid = current.id if current.role == "customer" else None
    stmt = select(AlertConfig)
    if cid is not None:
        stmt = stmt.where((AlertConfig.customer_id == cid) | (AlertConfig.customer_id.is_(None)))
    rows = (await db.execute(stmt.order_by(AlertConfig.id))).scalars().all()
    return ok([AlertConfigOut.model_validate(r).model_dump() for r in rows])


@router.post("/alerts")
async def add_alert(body: AlertConfigCreate, current=Depends(get_current_user), db: AsyncSession = Depends(get_db)):
    # 仅管理员可创建告警配置,客户无权自行配置
    if current.role != "admin":
        raise HTTPException(403, "仅管理员可配置告警,请联系管理员")
    cid = None
    # webhook_secret 为 None 时不传(用模型默认 ""),显式 "" 表示清空
    # ★ P2 修复: 使用显式允许字段白名单,防止 Mass Assignment
    _alert_data = {k: v for k, v in body.model_dump(exclude_none=True).items() if k in ALERT_UPDATABLE_FIELDS}
    cfg = AlertConfig(customer_id=cid, **_alert_data)
    db.add(cfg)
    try:
        await db.commit()
    except Exception:
        await db.rollback()
        logger.exception("创建告警配置失败")
        raise HTTPException(500, "创建告警配置失败,请稍后重试")
    return ok(AlertConfigOut.model_validate(cfg).model_dump())


@router.put("/alerts/{aid}")
async def update_alert(aid: int, body: AlertConfigCreate, current=Depends(get_current_user), db: AsyncSession = Depends(get_db)):
    # 仅管理员可修改告警配置
    if current.role != "admin":
        raise HTTPException(403, "仅管理员可修改告警配置")
    stmt = select(AlertConfig).where(AlertConfig.id == aid)
    cfg = (await db.execute(stmt)).scalar_one_or_none()
    if not cfg:
        raise HTTPException(404, "告警配置不存在")
    # webhook_secret=None 表示不修改(保留原值);"" 表示清空;其他=新值
    # ★ P2 修复: 使用显式允许字段白名单,防止 Mass Assignment
    for k, v in body.model_dump(exclude_none=True).items():
        if k in ALERT_UPDATABLE_FIELDS:
            setattr(cfg, k, v)
    try:
        await db.commit()
    except Exception:
        await db.rollback()
        logger.exception("更新告警配置失败")
        raise HTTPException(500, "更新告警配置失败,请稍后重试")
    return ok(AlertConfigOut.model_validate(cfg).model_dump())


@router.patch("/alerts/{aid}/toggle")
async def toggle_alert(aid: int, current=Depends(get_current_user), db: AsyncSession = Depends(get_db)):
    """切换告警配置的启用/停用状态。"""
    if current.role != "admin":
        raise HTTPException(403, "仅管理员可修改告警配置")
    stmt = select(AlertConfig).where(AlertConfig.id == aid)
    cfg = (await db.execute(stmt)).scalar_one_or_none()
    if not cfg:
        raise HTTPException(404, "告警配置不存在")
    cfg.enabled = not cfg.enabled
    try:
        await db.commit()
    except Exception:
        await db.rollback()
        logger.exception("切换告警状态失败")
        raise HTTPException(500, "切换告警状态失败,请稍后重试")
    return ok(AlertConfigOut.model_validate(cfg).model_dump())


@router.delete("/alerts/{aid}")
async def delete_alert(aid: int, current=Depends(get_current_user), db: AsyncSession = Depends(get_db)):
    # 仅管理员可删除告警配置
    if current.role != "admin":
        raise HTTPException(403, "仅管理员可删除告警配置")
    stmt = select(AlertConfig).where(AlertConfig.id == aid)
    cfg = (await db.execute(stmt)).scalar_one_or_none()
    if not cfg:
        raise HTTPException(404, "告警配置不存在")
    await db.delete(cfg)
    try:
        await db.commit()
    except Exception:
        await db.rollback()
        logger.exception("删除告警配置失败")
        raise HTTPException(400, "删除失败,请稍后重试")
    return ok({"message": "告警配置已删除", "id": aid})


@router.get("/alert-logs")
async def list_alert_logs(
    current=Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    from sqlalchemy import desc

    # 管理员可查看所有日志,客户只能查看与自己相关的告警日志
    if current.role == "admin":
        rows = (await db.execute(
            select(AlertLog).order_by(desc(AlertLog.id)).limit(100)
        )).scalars().all()
    else:
        # 客户:通过 AlertConfig 关联过滤
        from app.models.config import AlertConfig
        customer_alert_ids = (await db.execute(
            select(AlertConfig.id).where(
                AlertConfig.customer_id == current.id,
                AlertConfig.enabled.is_(True)
            )
        )).scalars().all()
        # 也包含全局告警(customer_id IS NULL)的日志
        global_alert_ids = (await db.execute(
            select(AlertConfig.id).where(AlertConfig.customer_id.is_(None))
        )).scalars().all()
        all_alert_ids = list(set(customer_alert_ids + global_alert_ids))
        
        if not all_alert_ids:
            return ok([])
        
        rows = (await db.execute(
            select(AlertLog)
            .where(AlertLog.alert_config_id.in_(all_alert_ids))
            .order_by(desc(AlertLog.id))
            .limit(100)
        )).scalars().all()
    
    return ok([
        {"id": r.id, "event": r.event, "title": r.title, "success": r.success,
         "created_at": r.created_at.isoformat() if r.created_at else None}
        for r in rows
    ])


# ---------- 客户急停开关(一键开启/停止) ----------
@router.post("/emergency-stop")
async def toggle_emergency_stop(current=Depends(require_customer), db: AsyncSession = Depends(get_db)):
    """客户一键急停:切换自己的 emergency_stop 开关。

    True = 阻断所有新开仓信号(平仓不受影响)
    False = 恢复正常跟单
    """
    cust = (
        await db.execute(select(Customer).where(Customer.id == current.id))
    ).scalar_one_or_none()
    if not cust:
        raise HTTPException(404, "客户不存在")
    cust.emergency_stop = not cust.emergency_stop
    try:
        await db.commit()
    except Exception:
        await db.rollback()
        logger.exception("切换急停状态失败")
        raise HTTPException(500, "切换急停状态失败,请稍后重试")
    new_state = cust.emergency_stop
    action = "开启急停(阻断开仓)" if new_state else "关闭急停(恢复跟单)"
    logger.info(f"客户急停切换: customer={current.id} username={current.username} action={action}")
    return ok({"emergency_stop": new_state, "message": action})
