"""WebSocket 路由:向客户端推送实时事件(信号/订单/持仓/告警)。"""
import asyncio

from fastapi import APIRouter, WebSocket, WebSocketDisconnect

from app.core.security import decode_token
from app.services.event_bus import bus

router = APIRouter()


@router.websocket("/ws")
async def websocket_endpoint(ws: WebSocket):
    await ws.accept()
    token = ws.query_params.get("token", "")
    try:
        payload = decode_token(token)
    except Exception:
        await ws.send_text('{"event":"error","data":"token无效"}')
        await ws.close()
        return

    role = payload.get("role")
    sub = payload.get("sub")
    topic = "admin" if role == "admin" else f"customer:{payload.get('customer_id', sub)}"
    q = bus.subscribe(topic)
    await ws.send_text(f'{{"event":"connected","data":"{topic}"}}')
    try:
        while True:
            try:
                msg = await asyncio.wait_for(q.get(), timeout=1.0)
                await ws.send_text(msg)
            except asyncio.TimeoutError:
                continue
    except WebSocketDisconnect:
        pass
    finally:
        bus.unsubscribe(topic, q)
