// 整套跑完的收尾清理：用例在共享的 dev 数据目录里建的知识库（本机 dev 栈与 CI
// 都指向 data/dev）不删掉会越积越多，下次跑用例时网格里躺着一堆"E2E 冒烟库"。
// 清理走 API（DELETE /api/kb/{id}）而不是再点一遍 UI：它不属于被测对象，用 UI
// 反而多一份抖动。任何失败只警告——后端已经被关掉、库已被手工删掉，都不该让
// 一次成功的冒烟跑变成红灯。
import { existsSync, readFileSync, rmSync } from "node:fs";
import path from "node:path";
import { fileURLToPath } from "node:url";
import { request } from "@playwright/test";

const HERE = path.dirname(fileURLToPath(import.meta.url));
const CREATED_KB_FILE = path.join(HERE, "..", "test-results", "created-kbs.json");
const API_URL = process.env.E2E_API_URL ?? "http://127.0.0.1:8100";

export default async function globalTeardown() {
  if (!existsSync(CREATED_KB_FILE)) return;
  let ids: string[] = [];
  try {
    ids = JSON.parse(readFileSync(CREATED_KB_FILE, "utf-8")) as string[];
  } catch {
    rmSync(CREATED_KB_FILE, { force: true });
    return;
  }
  const ctx = await request.newContext({ baseURL: API_URL });
  try {
    for (const id of ids) {
      try {
        const res = await ctx.delete(`/api/kb/${id}`);
        if (!res.ok()) {
          console.warn(`[e2e] 清理知识库 ${id} 失败：HTTP ${res.status()}`);
        }
      } catch (err) {
        console.warn(`[e2e] 清理知识库 ${id} 失败：${String(err)}`);
      }
    }
  } finally {
    await ctx.dispose();
    rmSync(CREATED_KB_FILE, { force: true });
  }
}
