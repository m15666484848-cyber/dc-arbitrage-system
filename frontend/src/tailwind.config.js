/** @type {import('tailwindcss').Config} */
export default {
  content: ["./index.html", "./src/**/*.{ts,tsx}"],
  darkMode: "class",
  theme: {
    extend: {
      colors: {
        bg: {
          DEFAULT: "#0a0e1a",
          soft: "#111729",
          card: "#151c30",
          hover: "#1c2540",
        },
        border: { DEFAULT: "#1f2a44", soft: "#2a3658" },
        accent: {
          DEFAULT: "#d4a843",
          glow: "#f0c95c",
        },
        profit: "#22c55e",
        loss: "#ef4444",
        warn: "#d4a843",
      },
      fontFamily: {
        sans: ["Inter", "system-ui", "-apple-system", "PingFang SC", "Microsoft YaHei", "sans-serif"],
        mono: ["JetBrains Mono", "ui-monospace", "monospace"],
      },
      boxShadow: {
        glow: "0 0 20px -2px rgba(212,168,67,0.5)",
        card: "0 8px 32px -8px rgba(0,0,0,0.6)",
      },
      backgroundImage: {
        "grid-pattern":
          "linear-gradient(rgba(99,102,241,0.05) 1px,transparent 1px),linear-gradient(90deg,rgba(99,102,241,0.05) 1px,transparent 1px)",
      },
    },
  },
  plugins: [],
};
