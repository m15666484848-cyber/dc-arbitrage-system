import { createContext, useCallback, useContext, useState, ReactNode } from "react";
import { createPortal } from "react-dom";
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

    const duration = type === "error" ? 8000 : type === "success" ? 6000 : 6500;
    setTimeout(() => setToasts((t) => t.filter((x) => x.id !== id)), duration);
  }, []);

  const icons = { success: CheckCircle2, error: AlertCircle, info: Info };
  const styles = {
    success: {
      label: "成功",
      card: "border-emerald-300 bg-emerald-950 text-white shadow-[0_24px_80px_rgba(0,0,0,0.65)]",
      icon: "text-emerald-200",
      badge: "bg-emerald-300 text-emerald-950",
    },
    error: {
      label: "失败",
      card: "border-red-300 bg-red-950 text-white shadow-[0_24px_80px_rgba(0,0,0,0.75)]",
      icon: "text-red-200",
      badge: "bg-red-300 text-red-950",
    },
    info: {
      label: "提示",
      card: "border-sky-300 bg-sky-950 text-white shadow-[0_24px_80px_rgba(0,0,0,0.65)]",
      icon: "text-sky-200",
      badge: "bg-sky-300 text-sky-950",
    },
  };

  const toastLayer = (
    <div
      className="fixed left-1/2 top-6 flex w-[calc(100vw-28px)] max-w-2xl -translate-x-1/2 flex-col gap-3 pointer-events-none"
      style={{ zIndex: 2147483647 }}
      aria-live="polite"
      aria-atomic="true"
    >
      {toasts.map((t) => {
        const Icon = icons[t.type];
        const style = styles[t.type];
        return (
          <div
            key={t.id}
            role={t.type === "error" ? "alert" : "status"}
            className={`pointer-events-auto flex items-start gap-3 rounded-2xl border-2 px-5 py-4 ${style.card}`}
            style={{ zIndex: 2147483647 }}
          >
            <Icon size={28} className={`mt-0.5 shrink-0 ${style.icon}`} />
            <div className="min-w-0 flex-1">
              <div className="mb-1.5 flex items-center gap-2">
                <span className={`rounded-full px-2.5 py-0.5 text-sm font-extrabold ${style.badge}`}>
                  {style.label}
                </span>
                <span className="text-sm font-semibold text-slate-200">操作结果</span>
              </div>
              <div className="break-words text-lg font-bold leading-7 text-white">{t.msg}</div>
            </div>
            <button
              onClick={() => setToasts((x) => x.filter((y) => y.id !== t.id))}
              className="touch-target -mr-2 -mt-2 rounded-full p-2 text-slate-200 hover:bg-white/15 hover:text-white"
              aria-label="关闭提示"
            >
              <X size={20} />
            </button>
          </div>
        );
      })}
    </div>
  );

  return (
    <Ctx.Provider value={{ push }}>
      {children}
      {typeof document !== "undefined" ? createPortal(toastLayer, document.body) : null}
    </Ctx.Provider>
  );
}

export const useToast = () => useContext(Ctx);
