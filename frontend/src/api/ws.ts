import { useAuthStore } from "@/stores/auth";

type WsListener = (event: string, data?: any) => void;
type Unsubscribe = () => void;

// S9修复: 仅从内存store获取token,移除localStorage回退
function getToken(): string | null {
  try {
    return useAuthStore.getState()?.token || null;
  } catch {
    return null;
  }
}

class WsClient {
  private ws: WebSocket | null = null;
  private listeners: Set<WsListener> = new Set();
  private reconnectTimer: any = null;
  private authTimer: any = null;
  private reconnectAttempts: number = 0;
  private manualClose: boolean = false;
  private static readonly MAX_RECONNECT_ATTEMPTS = 20;

  connect() {
    this.manualClose = false;
    // 关闭已有连接，防止 resetReconnect 产生僵尸连接
    if (this.ws) {
      this.ws.onclose = null;
      this.ws.onopen = null;
      this.ws.onmessage = null;
      this.ws.onerror = null;
      if (this.ws.readyState === WebSocket.OPEN || this.ws.readyState === WebSocket.CONNECTING) {
        this.ws.close();
      }
      this.ws = null;
    }
    const token = getToken();
    if (!token) {
      this.scheduleReconnect();
      return;
    }
    const url = import.meta.env.VITE_WS_URL || `${location.protocol === "https:" ? "wss" : "ws"}://${location.host}/ws`;
    try {
      this.ws = new WebSocket(url, [`bearer.${token}`]);

      this.ws.onopen = () => {
        this.reconnectAttempts = 0;
        if (this.authTimer) clearTimeout(this.authTimer);
        this.authTimer = setTimeout(() => {
          if (this.ws && this.ws.readyState === WebSocket.OPEN) {
            this.ws.close();
          }
        }, 5000);
      };

      this.ws.onmessage = (e) => {
        try {
          const msg = JSON.parse(e.data);
          const eventType = msg.event || msg.type;
          if (eventType === "connected") {
            if (this.authTimer) {
              clearTimeout(this.authTimer);
              this.authTimer = null;
            }
            this.emit("connected", msg.data);
          } else if (eventType === "error") {
            if (this.authTimer) {
              clearTimeout(this.authTimer);
              this.authTimer = null;
            }
          } else if (eventType === "heartbeat") {
            // Heartbeat, ignore
          } else {
            this.emit(eventType || "message", msg);
          }
        } catch {
          this.emit("message", e.data);
        }
      };

      this.ws.onclose = () => {
        if (this.manualClose) {
          return;
        }
        if (this.authTimer) {
          clearTimeout(this.authTimer);
          this.authTimer = null;
        }
        this.emit("disconnected");
        this.scheduleReconnect();
      };

      this.ws.onerror = () => {};
    } catch {
      this.scheduleReconnect();
    }
  }

  close() {
    this.manualClose = true;
    if (this.reconnectTimer) {
      clearTimeout(this.reconnectTimer);
      this.reconnectTimer = null;
    }
    if (this.authTimer) {
      clearTimeout(this.authTimer);
      this.authTimer = null;
    }
    if (this.ws) {
      this.ws.close();
      this.ws = null;
    }
  }

  on(listener: WsListener): Unsubscribe {
    this.listeners.add(listener);
    return () => {
      this.listeners.delete(listener);
    };
  }

  private emit(event: string, data?: any) {
    this.listeners.forEach((l) => l(event, data));
  }

  private scheduleReconnect() {
    if (this.manualClose) return;
    if (this.reconnectAttempts >= WsClient.MAX_RECONNECT_ATTEMPTS) {
      console.warn(`[WS] Max reconnection attempts (${WsClient.MAX_RECONNECT_ATTEMPTS}) reached, stopping.`);
      return;
    }
    if (this.reconnectTimer) return;
    // 指数退避: 1s, 2s, 4s, 8s, 16s, 最大 30s
    const delay = Math.min(1000 * Math.pow(2, this.reconnectAttempts), 30000);
    this.reconnectAttempts++;
    this.reconnectTimer = setTimeout(() => {
      this.reconnectTimer = null;
      this.connect();
    }, delay);
  }

  /** Manually reset reconnection counter and retry. Call after user login/token refresh. */
  resetReconnect() {
    this.reconnectAttempts = 0;
    if (this.reconnectTimer) {
      clearTimeout(this.reconnectTimer);
      this.reconnectTimer = null;
    }
    this.connect();
  }
}

export const wsClient = new WsClient();

/** Reset WS reconnection counter and reconnect. */
export function resetWsReconnect() {
  wsClient.resetReconnect();
}
