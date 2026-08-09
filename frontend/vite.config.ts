import { defineConfig } from "vite";
import react from "@vitejs/plugin-react";
import { resolve } from "node:path";

const vendorDeps = [
  "recharts",
  "lightweight-charts",
  "lucide-react",
];

export default defineConfig({
  plugins: [react()],
  resolve: {
    alias: {
      "@": resolve(__dirname, "./src"),
    },
  },
  server: {
    port: 5173,
    host: true,
    proxy: {
      "/api": {
        target: "http://localhost:8000",
        changeOrigin: true,
      },
      "/ws": {
        target: "ws://localhost:8000",
        ws: true,
      },
    },
  },
  build: {
    outDir: "dist",
    emptyOutDir: true,
    sourcemap: false,
    chunkSizeWarningLimit: 500,
    rollupOptions: {
      output: {
        manualChunks(id: string) {
          // Admin routes -> own chunk to keep initial bundle small
          if (id.includes("/src/pages/admin/")) {
            return "admin";
          }

          // Known heavy vendors -> isolated chunks
          for (const dep of vendorDeps) {
            if (id.includes(`node_modules/${dep}/`)) {
              return dep;
            }
          }

          // React family and all other node_modules -> vendor chunk
          if (id.includes("node_modules")) {
            return "vendor";
          }
        },
      },
    },
  },
});
