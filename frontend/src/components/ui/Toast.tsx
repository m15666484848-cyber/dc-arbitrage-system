import { createContext, useCallback, useContext, useState, ReactNode } from "react";
import { CheckCircle2, AlertCircle, Info, X } from "lucide-react";

type Toast = { id: number; type: "success" | "error" | "info"; msg: string };
const Ctx = createContext<{ push: (type: Toast["type"], msg: string) => void }>({
  push: () => {},
});

export function ToastProvider({ children }: { children: ReactNode }) {
  const [toasts, setToasts] = useState<Toast[]>([]);
  const push = useCallback((type: Toast["type"], msg: string) => {
    const id = Date.now() + Math.random();
    setToasts((t) => [...t, { id, type, msg }]);
    setTimeout(() => setToasts((t) => t.filter((x) => x.id !== id)), 4000);
  }, []);

  const icons = { success: CheckCircle2, error: AlertCircle, info: Info };
  const colors = {
    success: "border-profit/40 text-profit bg-profit/5",
    error: "border-loss/40 text-loss bg-loss/5",
    info: "border-accent/40 text-accent-glow bg-accent/5",
  };

  return (
    <Ctx.Provider value={{ push }}>
      {children}
      <div className="fixed top-4 right-4 left-4 md:left-auto z-[100] flex flex-col gap-2 md:max-w-sm pointer-events-none">
        {toasts.map((t) => {
          const Icon = icons[t.type];
          return (
            <div
              key={t.id}
              className={`glass px-4 py-3 flex items-start gap-2.5 border ${colors[t.type]} slide-in-right pointer-events-auto`}
            >
              <Icon size={18} className="mt-0.5 shrink-0" />
              <div className="text-sm text-slate-200 flex-1">{t.msg}</div>
              <button
                onClick={() => setToasts((x) => x.filter((y) => y.id !== t.id))}
                className="text-slate-500 hover:text-slate-300 touch-target"
                aria-label="关闭"
              >
                <X size={14} />
              </button>
            </div>
          );
        })}
      </div>
    </Ctx.Provider>
  );
}

export const useToast = () => useContext(Ctx);
