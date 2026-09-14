// 页面级端到端冒烟测试（Playwright，真实浏览器）。
//
// 放在 web-app/ 根而不是 web-app/e2e/：npm script 是 `playwright test`（零参数），
// Playwright 只在**当前目录**找 playwright.config.ts，配置放 e2e/ 里就得靠
// --config 才能被发现。testDir 指向 ./e2e，与用例目录一致。
//
// 用例文件用 *.e2e.ts 而不是 *.spec.ts：vitest 的默认 include 是
// `**/*.{test,spec}.*`，e2e/ 下的 .spec.ts 会被 `npm test` 一起收走并报错；
// 换后缀 + 下面的 testMatch 两边都不用改各自框架的配置。
import { existsSync } from "node:fs";
import path from "node:path";
import { fileURLToPath } from "node:url";
import { defineConfig, devices } from "@playwright/test";

const HERE = path.dirname(fileURLToPath(import.meta.url)); // web-app/
const REPO = path.resolve(HERE, "..");

// 端口与 vite.config.ts 的 proxy 对齐：Vite dev(5173) 把 /api、/healthz 反代到
// 后端 8100，浏览器只跟 5173 说话——所以两个进程都必须先起来。
// 两个 env 只为"本机 5173/8100 已被别的栈占用、另起一套"准备；挪端口时 Vite 的
// 代理目标也要跟着挪，否则界面能开、接口却打到了旧栈上。
const WEB_PORT = Number(process.env.E2E_WEB_PORT ?? 5173);
const API_PORT = Number(process.env.E2E_API_PORT ?? 8100);

// 后端解释器：CI 上就是 PATH 里的 python3；本机依赖装在项目约定的 venv 里
// （本机没有全局 python 环境，python3 是 /usr/bin 的系统解释器）。按候选清单
// 取第一个存在的，E2E_PYTHON 可无条件覆盖。
const PYTHON = [
  process.env.E2E_PYTHON,
  path.join(REPO, "..", "kbase-m5-1", ".venv-dev", "bin", "python"),
  path.join(HERE, ".venv-dev", "bin", "python"),
  path.join(REPO, ".venv-dev", "bin", "python"),
].find((p) => p !== undefined && existsSync(p)) ?? "python3";

export default defineConfig({
  testDir: "./e2e",
  testMatch: /.*\.e2e\.ts/,
  // 共享同一个 dev 后端数据目录（库/会话/文档都是全局状态）：串行跑，避免
  // 用例之间抢"当前知识库"、抢会话列表。单 worker 下跑的用例也不多。
  fullyParallel: false,
  workers: 1,
  forbidOnly: !!process.env.CI,
  // CI：重试 2 次（E2E 抖动不该直接红），首败留 trace；本机：不重试，抖动要
  // 就地暴露出来而不是被重试掩盖。
  retries: process.env.CI ? 2 : 0,
  reporter: [["list"], ["html", { open: "never" }]],
  timeout: 90_000,
  expect: { timeout: 20_000 },
  use: {
    // E2E_BASE_URL 可以把整套指向另一个前端栈（例如手工验证"完全无密钥"时用的
    // 桩后端 + 临时 Vite）；缺省就是本地/CI 的默认栈。
    baseURL: process.env.E2E_BASE_URL ?? `http://localhost:${WEB_PORT}`,
    // 断言的是中文文案（kbase-lang 走 localStorage → 浏览器语言 → 默认 zh）。
    // 不锁 locale 的话 Playwright 默认 en-US，界面会是英文。
    locale: "zh-CN",
    trace: "on-first-retry",
    screenshot: "only-on-failure",
    video: "off",
  },
  projects: [{ name: "chromium", use: { ...devices["Desktop Chrome"] } }],
  // 只装 Chromium（见 README）：冒烟看的是"页面坏没坏"，不需要跨浏览器矩阵。
  webServer: [
    {
      // 后端：dev 配置 + 假向量 embedder + auth=off + 确定性桩 LLM，
      // 不下载模型、不依赖任何外部 API（见 e2e/dev_server.py）。
      command: `"${PYTHON}" -m uvicorn --factory dev_server:create_dev_app --host 127.0.0.1 --port ${API_PORT}`,
      cwd: "./e2e",
      url: `http://127.0.0.1:${API_PORT}/healthz`,
      reuseExistingServer: true,
      timeout: 120_000,
    },
    {
      // --strictPort：端口被占时直接失败，而不是悄悄换到 5174——换了端口
      // Playwright 的健康检查仍会命中旧服务，用例就跑在另一个栈上了。
      command: `npm run dev -- --port ${WEB_PORT} --strictPort`,
      url: `http://localhost:${WEB_PORT}`,
      reuseExistingServer: true,
      timeout: 120_000,
    },
  ],
  globalTeardown: "./e2e/global-teardown.ts",
});
