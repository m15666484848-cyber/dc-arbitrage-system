import { ReactNode, SelectHTMLAttributes, InputHTMLAttributes, ButtonHTMLAttributes, ComponentType } from "react";
import { cn } from "@/lib/utils";

type IconComponent = ComponentType<{ size?: string | number; className?: string }>;

export function Button({
  variant = "primary",
  className,
  children,
  ...props
}: ButtonHTMLAttributes<HTMLButtonElement> & { variant?: "primary" | "ghost" | "danger" | "gold" }) {
  const variants = {
    primary: "btn-primary",
    ghost: "btn-ghost",
    danger: "btn-danger",
    gold: "btn-gold",
  };
  return (
    <button className={cn(variants[variant], "min-h-0 hover-lift focus-emerald", className)} {...props}>
      {children}
    </button>
  );
}

export function Card({
  className,
  children,
  variant = "premium",
}: {
  className?: string;
  children: ReactNode;
  variant?: "default" | "premium";
}) {
  const base = variant === "premium" ? "card-premium" : "glass card-hover";
  return <div className={cn(base, "p-5", className)}>{children}</div>;
}

export function CardTitle({ children, action }: { children: ReactNode; action?: ReactNode }) {
  return (
    <div className="flex items-center justify-between gap-3 mb-5">
      <h3 className="text-base font-bold text-text tracking-tight font-display">{children}</h3>
      {action}
    </div>
  );
}

export function Badge({
  children,
  tone = "default",
  className,
}: {
  children: ReactNode;
  tone?: "default" | "profit" | "loss" | "warn" | "accent" | "gold";
  className?: string;
}) {
  const tones = {
    default: "bg-white/[0.04] text-text-secondary border border-white/10",
    profit:
      "bg-profit/10 text-profit border border-profit/25 shadow-[0_0_12px_-3px_rgba(0,212,160,0.22)]",
    loss: "bg-loss/10 text-loss border border-loss/25 shadow-[0_0_12px_-3px_rgba(240,65,85,0.22)]",
    warn: "bg-gold/10 text-gold border border-gold/25 shadow-[0_0_12px_-3px_rgba(240,180,41,0.2)]",
    accent:
      "bg-accent/10 text-accent border border-accent/25 shadow-[0_0_12px_-3px_rgba(56,189,248,0.2)]",
    gold:
      "bg-gold/10 text-gold border border-gold/30 shadow-[0_0_12px_-3px_rgba(240,180,41,0.22)]",
  };
  return <span className={cn("chip", tones[tone], className)}>{children}</span>;
}

export function Input({ className, ...props }: InputHTMLAttributes<HTMLInputElement>) {
  return <input className={cn("input", className)} {...props} />;
}

export function Select({ className, children, ...props }: SelectHTMLAttributes<HTMLSelectElement>) {
  return (
    <select className={cn("input cursor-pointer", className)} {...props}>
      {children}
    </select>
  );
}

export function Label({ children }: { children: ReactNode }) {
  return <label className="label">{children}</label>;
}

export function Field({ label, children }: { label: string; children: ReactNode }) {
  return (
    <div>
      <Label>{label}</Label>
      {children}
    </div>
  );
}

export function Stat({
  label,
  value,
  sub,
  tone = "default",
  trend,
  icon: Icon,
  className,
  bordered = false,
}: {
  label: string;
  value: ReactNode;
  sub?: ReactNode;
  tone?: "default" | "profit" | "loss" | "accent" | "gold";
  trend?: "up" | "down" | "neutral";
  icon?: IconComponent;
  className?: string;
  bordered?: boolean;
}) {
  const toneCls =
    tone === "profit"
      ? "text-profit"
      : tone === "loss"
        ? "text-loss"
        : tone === "accent"
          ? "text-accent"
          : tone === "gold"
            ? "text-gold"
            : "text-text";
  const trendColor =
    trend === "up" ? "text-up" : trend === "down" ? "text-down" : "text-text-tertiary";
  const toneBorder =
    tone === "profit"
      ? "border-l-profit/50"
      : tone === "loss"
        ? "border-l-loss/50"
        : tone === "accent"
          ? "border-l-accent/50"
          : tone === "gold"
            ? "border-l-gold/50"
            : "border-l-border/50";
  const toneClass =
    tone === "profit"
      ? "profit"
      : tone === "loss"
        ? "loss"
        : tone === "gold"
          ? "gold"
          : tone === "accent"
            ? "accent"
            : "";

  return (
    <div
      className={cn(
        "stat card-hover relative overflow-hidden group p-4 md:p-5",
        toneClass,
        bordered && `border-l-[3px] ${toneBorder}`,
        className
      )}
    >
      <div className="flex items-center justify-between mb-2">
        <div className="stat-label truncate">{label}</div>
        {Icon && (
          <div className="stat-icon-wrap">
            <Icon size={17} className="text-text-tertiary group-hover:text-text-secondary transition-colors" />
          </div>
        )}
      </div>
      <div className={cn("stat-value truncate", toneCls)}>{value}</div>
      {sub && (
        <div className="flex items-center gap-1.5 text-[11px] md:text-xs text-text-tertiary mt-1 md:mt-1.5 truncate">
          {trend && (
            <span className={cn("font-bold", trendColor)}>
              {trend === "up" ? "↑" : trend === "down" ? "↓" : "—"}
            </span>
          )}
          {sub}
        </div>
      )}
    </div>
  );
}

export function Empty({ text = "暂无数据" }: { text?: string }) {
  return (
    <div className="py-14 text-center text-text-tertiary text-sm animate-fadeIn">
      <div className="w-16 h-16 mx-auto mb-4 rounded-2xl card-premium premium-glow flex items-center justify-center text-3xl">
        <span className="gradient-text opacity-80">∅</span>
      </div>
      <p>{text}</p>
    </div>
  );
}

export function SectionHeader({
  title,
  subtitle,
  icon: Icon,
  action,
}: {
  title: string;
  subtitle?: string;
  icon?: IconComponent;
  action?: ReactNode;
}) {
  return (
    <div className="flex flex-col sm:flex-row sm:items-center justify-between gap-3">
      <div>
        <h1 className="text-xl font-bold gradient-text flex items-center gap-2 font-display">
          {Icon && <Icon size={20} />}
          {title}
        </h1>
        {subtitle && <p className="text-sm text-text-tertiary mt-1">{subtitle}</p>}
      </div>
      {action && <div className="flex items-center gap-2 flex-wrap">{action}</div>}
    </div>
  );
}

export function MetricCard({
  label,
  value,
  sub,
  tone = "default",
  trend,
  icon: Icon,
}: {
  label: string;
  value: ReactNode;
  sub?: ReactNode;
  tone?: "default" | "profit" | "loss" | "accent" | "gold";
  trend?: "up" | "down" | "neutral";
  icon?: IconComponent;
}) {
  return (
    <Stat
      label={label}
      value={value}
      sub={sub}
      tone={tone}
      trend={trend}
      icon={Icon}
      bordered
    />
  );
}

export function MobileDataCard({
  header,
  badge,
  rows,
  footer,
}: {
  header: ReactNode;
  badge?: ReactNode;
  rows: { label: string; value: ReactNode; mono?: boolean }[];
  footer?: ReactNode;
}) {
  return (
    <div className="glass-soft p-3.5 card-hover">
      <div className="flex items-start justify-between gap-2 mb-3">
        <div className="min-w-0 flex-1">{header}</div>
        {badge && <div className="shrink-0">{badge}</div>}
      </div>
      <div className="grid grid-cols-2 gap-2 text-xs">
        {rows.map((row, i) => (
          <div key={i}>
            <div className="text-text-tertiary text-[11px]">{row.label}</div>
            <div className={cn("text-text", row.mono && "font-mono")}>{row.value}</div>
          </div>
        ))}
      </div>
      {footer && <div className="mt-3 pt-3 border-t border-border/40">{footer}</div>}
    </div>
  );
}
