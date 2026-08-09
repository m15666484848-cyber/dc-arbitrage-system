import { create } from "zustand";
import { persist } from "zustand/middleware";

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

export const useAuthStore = create<AuthState>()(
  persist(
    (set) => ({
      token: null,
      user: null,
      initialized: false,
      setAuth: (token, user) => set({ token, user, initialized: true }),
      setUser: (user) => set({ user, initialized: true }),
      logout: () => set({ token: null, user: null, initialized: true }),
      markInitialized: () => set({ initialized: true }),
    }),
    { name: "dc-quant-auth" }
  )
);
