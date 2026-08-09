"""健康检查。"""
from fastapi import APIRouter, Depends

from app.core.security import get_current_user

router = APIRouter()


@router.get("/health")
async def health():
    return {"status": "ok"}


@router.get("/health/deep")
async def health_deep(current=Depends(get_current_user)):
    """深度健康检查:返回后台任务存活状态(需登录)。

    用于运维监控:检测 Discord 监听 / 持仓监控 / 待触发单监控 / 看门狗 是否正常运行,
    任一循环退出时看门狗会自动重启,此处反映实时状态便于排查。
    """
    from app.workers.background import get_background_tasks_status

    tasks = get_background_tasks_status()
    all_alive = all(t.get("alive") for t in tasks.values()) if tasks else False
    return {
        "status": "ok" if all_alive else "degraded",
        "background_tasks": tasks,
    }


@router.get("/source-status")
async def source_status(current=Depends(get_current_user)):
    """转发源简要状态。

    客户端只需要红黄绿灯,不暴露管理员诊断详情。
    """
    from app.services.discord_monitor import get_source_status

    status = get_source_status()
    configured = bool(status.get("configured"))
    healthy = bool(status.get("healthy"))
    if not configured:
        level = "yellow"
        label = "转发源未配置"
    elif healthy:
        level = "green"
        label = "转发源正常"
    else:
        level = "red"
        label = "转发源异常"

    return {
        "level": level,
        "label": label,
        "healthy": healthy,
        "configured": configured,
        "connected": bool(status.get("connected")),
        "last_heartbeat_ack_at": status.get("last_heartbeat_ack_at"),
        "last_message_at": status.get("last_message_at"),
        "updated_at": status.get("updated_at"),
    }
