"""策略管理路由:创建/编辑/列表/删除策略,管理员可查看所有,客户只能看自己。"""
from fastapi import APIRouter, Depends, HTTPException
from loguru import logger
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.database import get_db
from app.core.security import get_current_user, require_customer
from app.models.strategy import Strategy
from app.schemas.common import ok
from app.schemas.strategy import StrategyCreate, StrategyOut

router = APIRouter(prefix="/strategies", tags=["策略"])


@router.get("")
async def list_strategies(current=Depends(get_current_user), db: AsyncSession = Depends(get_db)):
    if current.role == "admin":
        rows = (await db.execute(select(Strategy).where(Strategy.enabled.is_(True)).order_by(Strategy.id))).scalars().all()
    else:
        rows = (await db.execute(select(Strategy).where(Strategy.customer_id == current.id, Strategy.enabled.is_(True)).order_by(Strategy.id))).scalars().all()
    return ok([StrategyOut.model_validate(s).model_dump() for s in rows])


@router.post("")
async def create_strategy(
    body: StrategyCreate, current=Depends(require_customer), db: AsyncSession = Depends(get_db)
):
    s = Strategy(
        customer_id=current.id,
        name=body.name,
        type=body.type,
        params=body.params.model_dump(),
        enabled=body.enabled,
    )
    db.add(s)
    try:
        await db.commit()
    except Exception as e:
        await db.rollback()
        logger.exception(f"创建策略失败: {e}")
        raise HTTPException(500, "创建策略失败")
    return ok(StrategyOut.model_validate(s).model_dump())


@router.put("/{sid}")
async def update_strategy(
    sid: int, body: StrategyCreate, current=Depends(require_customer), db: AsyncSession = Depends(get_db)
):
    s = (await db.execute(select(Strategy).where(Strategy.id == sid, Strategy.customer_id == current.id))).scalar_one_or_none()
    if not s:
        raise HTTPException(404, "策略不存在")
    s.name = body.name
    s.type = body.type
    s.params = body.params.model_dump()
    s.enabled = body.enabled
    try:
        await db.commit()
    except Exception as e:
        await db.rollback()
        logger.exception(f"更新策略失败 sid={sid}: {e}")
        raise HTTPException(500, "更新策略失败")
    return ok(StrategyOut.model_validate(s).model_dump())


@router.delete("/{sid}")
async def delete_strategy(sid: int, current=Depends(require_customer), db: AsyncSession = Depends(get_db)):
    s = (await db.execute(select(Strategy).where(Strategy.id == sid, Strategy.customer_id == current.id))).scalar_one_or_none()
    if not s:
        raise HTTPException(404, "策略不存在")
    # 检查是否有关联的KOL关注
    from sqlalchemy import func, select as sa_select
    from app.models.kol import KolFollow
    follow_count = (await db.execute(
        sa_select(func.count()).select_from(KolFollow).where(KolFollow.strategy_id == sid)
    )).scalar() or 0

    # 实际删除策略(关联的kol_follows.strategy_id会自动SET NULL)
    await db.delete(s)
    try:
        await db.commit()
    except Exception as e:
        await db.rollback()
        logger.exception(f"删除策略失败 sid={sid}: {e}")
        raise HTTPException(500, "删除策略失败")

    # 失效策略缓存
    try:
        from app.services.order_manager import _invalidate_strategy_cache
        _invalidate_strategy_cache(s.customer_id, None)
    except Exception:
        logger.opt(exception=True).debug("ignored exception")

    return ok({"id": sid, "deleted": True, "unlinked_follows": follow_count})
