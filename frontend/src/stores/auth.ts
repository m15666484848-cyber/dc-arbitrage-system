import { create } from "zustand";

interface AuthUser {
  id: number;
  username: string;
  role: "admin" | "customer";
  display_name?: string;
  authorization?: { authorized: boolean; expires_at: string | null };
  show_signal_summary?: boolean;
  emergency_stop?: boolean;
}

interface AuthState {
  token: string | null;
  user: AuthUser | null;
  initialized: boolean;
  setAuth: (token: string, user: AuthUser) => void;
  setUser: (user: AuthUser) => void;
  logout: () => void;
  markInitialized: () => void;
}

// S9修复: 移除persist中间件,token仅存储在内存中
// 刷新页面时通过 /auth/refresh (HttpOnly Cookie) 重新获取access token
export const useAuthStore = create<AuthState>()((set) => ({
  token: null,
  user: null,
  initialized: false,
  setAuth: (token, user) => set({ token, user, initialized: true }),
  setUser: (user) => set({ user, initialized: true }),
  logout: () => set({ token: null, user: null, initialized: true }),
  markInitialized: () => set({ initialized: true }),
}));
