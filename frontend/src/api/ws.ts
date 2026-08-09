import { useAuthStore } from "@/stores/auth";

type WsListener = (event: string, data?: any) => void;
type Unsubscribe = () => void;

/** 获取token: 优先从Zustand store读取,回退到localStorage(防止store未初始化) */
function getToken(): string | null {
  try {
    const token = useAuthStore.getState()?.token;
    if (token) return token;
  } catch {}
  try {
    const stored = JSON.parse(localStorage.getItem("dc-quant-auth") || "{}");
    return stored?.state?.token || null;
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

  connect() {
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
    if (this.reconnectTimer) return;
    // 指数退避: 1s, 2s, 4s, 8s, 16s, 最大 30s
    const delay = Math.min(1000 * Math.pow(2, this.reconnectAttempts), 30000);
    this.reconnectAttempts++;
    this.reconnectTimer = setTimeout(() => {
      this.reconnectTimer = null;
      this.connect();
    }, delay);
  }
}

export const wsClient = new WsClient();
