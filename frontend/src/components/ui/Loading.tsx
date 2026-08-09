import { Loader2 } from "lucide-react";

interface LoadingProps {
  text?: string;
  className?: string;
}

export function Loading({ text = "加载中...", className = "" }: LoadingProps) {
  return (
    <div className={`flex flex-col items-center justify-center gap-3 p-8 ${className}`}>
      <Loader2 size={28} className="text-gold animate-spin" />
      <span className="text-sm text-slate-400">{text}</span>
    </div>
  );
}

export function PageSkeleton() {
  return (
    <div className="min-h-[50vh] flex flex-col items-center justify-center gap-4 animate-fadeIn">
      <div className="relative w-12 h-12">
        <div className="absolute inset-0 rounded-xl bg-gradient-to-br from-gold to-gold-dim opacity-20 animate-pulse" />
        <Loader2 size={28} className="absolute inset-0 m-auto text-gold animate-spin" />
      </div>
      <p className="text-sm text-slate-400">正在初始化页面...</p>
    </div>
  );
}
