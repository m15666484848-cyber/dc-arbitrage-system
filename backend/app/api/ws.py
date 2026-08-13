"""WebSocket 路由:向客户端推送实时事件(信号/订单/持仓/告警)。"""
import asyncio
import time

from fastapi import APIRouter, WebSocket, WebSocketDisconnect
from loguru import logger
from sqlalchemy import select

from app.core.security import decode_token
from app.core.database import AsyncSessionLocal
from app.models.customer import Customer
from app.services.event_bus import bus



router = APIRouter()



HEARTBEAT_INTERVAL = 25  # 心跳间隔(秒),小于 nginx 超时(60s)





def _extract_token(ws: WebSocket) -> tuple[str, str]:

    """从 Sec-WebSocket-Protocol 子协议提取 token,回退到查询参数(过渡期兼容旧前端)。



    使用子协议传递 token 可避免 token 出现在 URL 查询参数中,从而避免被 nginx access

    log 记录。客户端以 "bearer.<jwt>" 作为子协议发起握手。



    返回 (token, client_subprotocol):client_subprotocol 用于在 accept 时回显,

    符合 RFC 6455(服务端必须返回客户端请求的子协议之一,否则浏览器会拒绝握手)。

    """

    raw = ws.headers.get("sec-websocket-protocol", "")

    if raw:

        for sp in raw.split(","):

            sp = sp.strip()

            if sp.startswith("bearer."):

                return sp[len("bearer."):], sp

    # 兼容旧前端:查询参数

    return ws.query_params.get("token", ""), ""





# S16修复: WebSocket 握手阶段 IP 频率限制
WS_RATE_LIMIT_MAX = 30  # 每分钟最多 30 次连接
WS_RATE_LIMIT_WINDOW = 60  # 窗口 60 秒


async def _check_ws_rate_limit(client_ip: str) -> bool:
    """检查 WebSocket 连接频率,防止暴力探测 token。"""
    try:
        from app.core.redis import redis_client
        key = f"ws_rate:{client_ip}"
        current = await redis_client.incr(key)
        if current == 1:
            await redis_client.expire(key, WS_RATE_LIMIT_WINDOW)
        return current <= WS_RATE_LIMIT_MAX
    except Exception:
        # Redis 不可用时放行,不影响正常连接
        return True


@router.websocket("/ws")
async def websocket_endpoint(ws: WebSocket):
    # S16修复: IP 频率限制
    client_ip = ws.client.host if ws.client else "unknown"
    if not await _check_ws_rate_limit(client_ip):
        await ws.close(code=1008)  # Policy Violation
        return

    token, client_sp = _extract_token(ws)
    # Reject connection before accepting if no token provided
    if not token:
        await ws.close(code=1008)  # Policy Violation
        return
    # 回显客户端请求的子协议以完成握手;无子协议时为 None
    await ws.accept(subprotocol=client_sp or None)
    # M12修复: 将accept后的WebSocket交互放入try/except,防止客户端提前断开导致未处理异常
    try:
        try:
            payload = decode_token(token)
        except Exception:
            await ws.send_text('{"event":"error","data":"token无效"}')
            await ws.close()
            return

        role = payload.get("role")
        sub = payload.get("sub")
        # Validate required payload fields
        if not role or not sub:
            await ws.send_text('{"event":"error","data":"token载荷无效"}')
            await ws.close()
            return

        # 客户连接时检查激活状态,未激活的客户不允许建立 WebSocket 连接
        if role == "customer":
            customer_id = payload.get("customer_id", sub)
            if not str(customer_id).isdigit():
                customer_id = None
            if customer_id:
                try:
                    async with AsyncSessionLocal() as db:
                        cust = (await db.execute(
                            select(Customer).where(Customer.id == int(customer_id))
                        )).scalar_one_or_none()
                        if not cust or not cust.is_active:
                            await ws.send_text('{"event":"error","data":"账户未激活"}')
                            await ws.close()
                            return
                except Exception as e:
                    # M3修复: 鉴权相关检查失败时拒绝连接,不允许降级放行
                    logger.warning(f"客户激活状态检查失败,拒绝连接: {e}")
                    await ws.send_text('{"event":"error","data":"账户状态检查失败,请稍后重试"}')
                    await ws.close()
                    return
    except WebSocketDisconnect:
        return
    except Exception as e:
        logger.warning(f"WebSocket握手阶段异常: {e}")
        return

    

    topic = "admin" if role == "admin" else f"customer:{payload.get('customer_id', sub)}"
    last_heartbeat = time.time()
    q = None
    try:
        q = bus.subscribe(topic)
        await ws.send_text(f'{{"event":"connected","data":"{topic}"}}')
        while True:
            try:
                msg = await asyncio.wait_for(q.get(), timeout=1.0)
                # P2 修复: 捕获 send_text 的所有异常(不仅是 WebSocketDisconnect)
                try:
                    await ws.send_text(msg)
                except WebSocketDisconnect:
                    break
                except Exception as e:
                    logger.warning(f"WebSocket send_text 失败,断开连接: {e}")
                    break
            except asyncio.TimeoutError:
                # 超时:检查是否需要发送心跳
                now = time.time()
                if now - last_heartbeat >= HEARTBEAT_INTERVAL:
                    try:
                        await ws.send_text('{"event":"heartbeat","data":null}')
                        last_heartbeat = now
                    except WebSocketDisconnect:
                        break
                    except Exception as e:
                        logger.warning(f"WebSocket 心跳发送失败,断开连接: {e}")
                        break
                continue
    except WebSocketDisconnect:
        pass
    except Exception as e:
        logger.warning(f"WebSocket 主循环异常: {e}")
    finally:
        if q is not None:
            bus.unsubscribe(topic, q)

