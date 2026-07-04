/// <reference types="vitest/config" />
import { defineConfig } from "vitest/config";
import vue from "@vitejs/plugin-vue";
import tailwindcss from "@tailwindcss/vite";
import path from "node:path";

export default defineConfig({
  plugins: [vue(), tailwindcss()],
  resolve: { alias: { "@": path.resolve(__dirname, "src") } },
  build: { outDir: "../web", emptyOutDir: true },
  server: { proxy: { "/api": "http://localhost:8100", "/healthz": "http://localhost:8100" } },
  test: { environment: "jsdom" },
});
