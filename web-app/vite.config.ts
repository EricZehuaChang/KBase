/// <reference types="vitest/config" />
import { defineConfig } from "vitest/config";
import vue from "@vitejs/plugin-vue";
import tailwindcss from "@tailwindcss/vite";
import path from "node:path";

export default defineConfig({
  plugins: [vue(), tailwindcss()],
  resolve: { alias: { "@": path.resolve(__dirname, "src") } },
  build: {
    outDir: "../web",
    emptyOutDir: true,
    // M5-1 F1：双 HTML 入口（Vite 多页应用构建）——index.html 编译成使用端
    // bundle（挂载 src/portal/main.ts），admin.html 编译成管理端 bundle
    // （挂载 src/admin/main.ts）。两个入口各自的静态 import 图互不相交是
    // "前端隔离"这条纵深防御的物理基础（唯一的强制边界仍是后端
    // require_role，见 spec §3.3）。manifest: true 额外产出
    // web/.vite/manifest.json，记录每个入口/chunk 的依赖图，供
    // scripts/check-bundle-isolation.mjs 在构建后做可达性校验——不装这个
    // manifest 就没有机器可读的依据去断言"使用端确实不含管理端代码"，只能
    // 靠人工审查 import 语句，容易漏。
    manifest: true,
    rollupOptions: {
      input: {
        main: path.resolve(__dirname, "index.html"),
        admin: path.resolve(__dirname, "admin.html"),
      },
    },
  },
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
