/** @type {import('tailwindcss').Config} */
export default {
  content: ["./index.html", "./src/**/*.{ts,tsx}"],
  darkMode: "class",
  theme: {
    extend: {
      colors: {
        bg: {
          DEFAULT: "#05060a",
          soft: "#0b0d11",
          card: "#0d0f14",
          hover: "#161924",
          elevated: "#111520",
          1: "#0b0d11",
          2: "#0f1117",
          3: "#13161e",
        },
        border: {
          DEFAULT: "rgba(255,255,255,0.055)",
          soft: "rgba(255,255,255,0.032)",
          hover: "rgba(255,255,255,0.1)",
          glow: "rgba(0, 212, 160, 0.16)",
        },
        accent: {
          DEFAULT: "#38bdf8",
          glow: "#38bdf8",
          dim: "#0ea5e9",
          soft: "rgba(56, 189, 248, 0.12)",
        },
        emerald: {
          DEFAULT: "#00d4a0",
          dim: "#00af86",
          glow: "rgba(0, 212, 160, 0.1)",
          border: "rgba(0, 212, 160, 0.16)",
        },
        gold: {
          DEFAULT: "#f0b429",
          glow: "#f0b429",
          dim: "#d4a020",
          soft: "rgba(240, 180, 41, 0.1)",
        },
        violet: {
          DEFAULT: "#7c5cfc",
        },
        sky: {
          DEFAULT: "#38bdf8",
        },
        rose: {
          DEFAULT: "#f04155",
          dim: "#d12e41",
        },
        up: "#f04155",
        down: "#00d4a0",
        profit: "#00d4a0",
        loss: "#f04155",
        warn: "#f0b429",
        text: {
          DEFAULT: "#e2e8f0",
          secondary: "#a0aec0",
          tertiary: "#6b7a8f",
          muted: "#48535f",
        },
      },
      fontFamily: {
        sans: [
          "Inter",
          "Sora",
          "system-ui",
          "-apple-system",
          "PingFang SC",
          "Microsoft YaHei",
          "sans-serif",
        ],
        display: ["Sora", "Inter", "system-ui", "sans-serif"],
        mono: ["JetBrains Mono", "ui-monospace", "SFMono-Regular", "monospace"],
      },
      boxShadow: {
        glow: "0 0 20px -4px rgba(56, 189, 248, 0.2)",
        "glow-sm": "0 0 14px -3px rgba(0, 212, 160, 0.18)",
        "glow-premium": "0 0 28px -6px rgba(56, 189, 248, 0.22)",
        "glow-emerald": "0 0 20px -4px rgba(0, 212, 160, 0.25)",
        "glow-gold": "0 0 20px -4px rgba(240, 180, 41, 0.22)",
        card: "0 1px 4px rgba(0, 0, 0, 0.45)",
        "card-hover": "0 4px 16px rgba(0, 0, 0, 0.4)",
        "card-lg": "0 10px 32px rgba(0, 0, 0, 0.55)",
      },
      backgroundImage: {
        "grid-pattern":
          "linear-gradient(rgba(255,255,255,0.008) 1px, transparent 1px), linear-gradient(90deg, rgba(255,255,255,0.008) 1px, transparent 1px)",
        shine:
          "linear-gradient(105deg, transparent 40%, rgba(255,255,255,0.08) 45%, rgba(255,255,255,0.15) 50%, rgba(255,255,255,0.08) 55%, transparent 60%)",
        "gradient-radial": "radial-gradient(var(--tw-gradient-stops))",
      },
      animation: {
        shimmer: "shimmer 3s ease-in-out infinite",
        pulseSoft: "pulseSoft 2s ease-in-out infinite",
        fadeIn: "fadeIn 0.4s ease-out",
        slideUp: "slideUp 0.4s ease-out",
        scaleIn: "scaleIn 0.2s cubic-bezier(0.16, 1, 0.3, 1)",
      },
      keyframes: {
        shimmer: {
          "0%": { backgroundPosition: "-200% 0" },
          "100%": { backgroundPosition: "200% 0" },
        },
        pulseSoft: {
          "0%, 100%": { opacity: "1" },
          "50%": { opacity: "0.6" },
        },
        fadeIn: {
          from: { opacity: "0", transform: "translateY(8px)" },
          to: { opacity: "1", transform: "translateY(0)" },
        },
        slideUp: {
          from: { opacity: "0", transform: "translateY(16px)" },
          to: { opacity: "1", transform: "translateY(0)" },
        },
        scaleIn: {
          from: { opacity: "0", transform: "scale(0.96)" },
          to: { opacity: "1", transform: "scale(1)" },
        },
      },
      transitionTimingFunction: {
        "out-expo": "cubic-bezier(0.16, 1, 0.3, 1)",
      },
    },
  },
  plugins: [],
};
