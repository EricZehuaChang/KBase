/// <reference types="vitest/config" />
import { defineConfig } from "vitest/config";
import vue from "@vitejs/plugin-vue";
import tailwindcss from "@tailwindcss/vite";
import path from "node:path";

export default defineConfig({
  plugins: [vue(), tailwindcss()],
  resolve: { alias: { "@": path.resolve(__dirname, "src") } },
  build: { outDir: "../web", emptyOutDir: true },
  // changeOrigin: false（显式，覆盖 Vite 默认值）——鉴权上线后后端 Origin 同源
  // 中间件（kbase/auth/deps.py make_origin_guard_middleware）比较 Origin 头
  // 与请求 Host 是否一致；若代理改写 Host 为后端地址（8100），会与浏览器发出
  // 的真实 Origin（5173）不匹配，导致开发环境下所有非 GET 请求（含登录）
  // 都被 403。保持 Host 透传为前端源，两者一致即可通过校验。
  server: {
    proxy: {
      "/api": { target: "http://localhost:8100", changeOrigin: false },
      "/healthz": { target: "http://localhost:8100", changeOrigin: false },
    },
  },
  test: { environment: "jsdom" },
});
