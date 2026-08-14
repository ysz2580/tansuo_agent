import path from "node:path"
import { defineConfig } from "vite"
import react from "@vitejs/plugin-react"
import tailwindcss from "@tailwindcss/vite"

// https://vite.dev/config/
export default defineConfig({
  plugins: [react(), tailwindcss()],
  resolve: {
    alias: {
      "@": path.resolve(import.meta.dirname, "./src"),
    },
  },
  server: {
    // 显式绑 IPv4 loopback：默认只绑 [::1]，http://127.0.0.1:5173 会拒连
    host: "127.0.0.1",
    proxy: {
      // 开发模式下把 /api 代理到 FastAPI 后端（python cli.py web）
      "/api": {
        target: "http://127.0.0.1:8000",
        changeOrigin: true,
      },
    },
  },
})
