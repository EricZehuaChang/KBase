#!/usr/bin/env node
// scripts/check-bundle-isolation.mjs —— M5-1 F1 的核心验收：机械校验"使用端
// 构建产物不含管理端代码"，而不是只靠人工审查 import 语句（容易漏）。
//
// 原理：vite.config.ts 开了 build.manifest（见该文件注释），构建后产出
// web/.vite/manifest.json——记录每个入口/chunk 的依赖图（imports=静态导入，
// dynamicImports=懒加载）。本脚本从使用端入口 "index.html" 出发做一次可达性
// 遍历（BFS，静态+动态导入都算，宁可查得严一点），断言遍历结果里不包含任何
// 管理端专属模块。
//
// 管理端专属视图（KbView/AnalysisView/GenerateView/SettingsView）在
// src/admin/router.ts 里必须用路由级懒加载（() => import(...)）接线——这是
// 本脚本能生效的前提：只有懒加载才会被打包器切成独立 chunk、在 manifest 里
// 留下可寻址的条目；换成文件顶部静态 import 会被直接内联进 admin 入口自己
// 的 chunk，届时既无法在 manifest 里定位它们，本脚本的"反向检查"（见下方
// assertAdminViewsAreSeparateChunks）也会立刻失败并给出明确提示，而不是
// 因为"压根没查到东西"而误判通过。
import { existsSync, readFileSync } from "node:fs";
import path from "node:path";

const projectRoot = path.resolve(import.meta.dirname, ".."); // web-app/
const repoRoot = path.resolve(projectRoot, "..");             // kbase-m5-1/（vite outDir = ../web）

const MANIFEST_CANDIDATES = [
  path.join(repoRoot, "web", ".vite", "manifest.json"),
  path.join(repoRoot, "web", "manifest.json"), // 兼容 build.manifest 输出位置随 Vite 版本变化
];

const PORTAL_ENTRY_KEY = "index.html";
const ADMIN_ENTRY_KEY = "admin.html";

// 管理端专属视图：必须能在 manifest 里各自找到独立条目（见文件头注释），
// 这份清单同时也是"使用端不可达"断言要排查的对象。
const ADMIN_ONLY_VIEW_KEYS = [
  "src/views/KbView.vue",
  "src/views/AnalysisView.vue",
  "src/views/GenerateView.vue",
  "src/views/SettingsView.vue",
];

function isAdminOnlyKey(key) {
  return key === ADMIN_ENTRY_KEY
    || key.startsWith("src/admin/")
    || ADMIN_ONLY_VIEW_KEYS.includes(key);
}

function loadManifest() {
  const found = MANIFEST_CANDIDATES.find(existsSync);
  if (!found) {
    console.error("[check-isolation] 找不到构建产物 manifest，候选路径：");
    for (const p of MANIFEST_CANDIDATES) console.error("  - " + p);
    console.error("请先执行 `npm run build`（vite.config.ts 需要 build.manifest: true）。");
    process.exit(1);
  }
  return { manifest: JSON.parse(readFileSync(found, "utf-8")), manifestPath: found };
}

/** 从 entryKey 出发做可达性 BFS，static imports 与 dynamicImports 都算——
 * 使用端理论上不该有任何路径（哪怕是懒加载）通向管理端模块，宁可查严。 */
function collectReachable(manifest, entryKey) {
  const seen = new Set();
  const stack = [entryKey];
  while (stack.length > 0) {
    const key = stack.pop();
    if (seen.has(key)) continue;
    seen.add(key);
    const chunk = manifest[key];
    if (!chunk) continue;
    for (const imp of chunk.imports ?? []) stack.push(imp);
    for (const imp of chunk.dynamicImports ?? []) stack.push(imp);
  }
  return seen;
}

function main() {
  const { manifest, manifestPath } = loadManifest();

  if (!manifest[PORTAL_ENTRY_KEY] || !manifest[ADMIN_ENTRY_KEY]) {
    console.error(`[check-isolation] manifest 缺少入口条目（期望 "${PORTAL_ENTRY_KEY}" 与 ` +
      `"${ADMIN_ENTRY_KEY}"），检查 vite.config.ts 的 build.rollupOptions.input 是否正确。`);
    console.error("实际 manifest keys: " + Object.keys(manifest).join(", "));
    process.exit(1);
  }

  // 反向检查：管理端专属视图必须各自是独立 chunk（否则说明 admin/router.ts
  // 把它们改成了静态 import，本脚本后面的正向检查会因为"查无此模块"而失去
  // 意义——必须先在这里显式失败，而不是悄悄放行）。
  const missingChunks = ADMIN_ONLY_VIEW_KEYS.filter((key) => !manifest[key]);
  if (missingChunks.length > 0) {
    console.error("[check-isolation] FAIL: 以下管理端视图没有在 manifest 里生成独立 chunk，" +
      "很可能被改成了静态 import（应改回 admin/router.ts 里的路由级懒加载 () => import(...)）：");
    for (const key of missingChunks) console.error("  - " + key);
    process.exit(1);
  }

  // 正向检查：使用端入口的可达性遍历（静态+动态导入）不应触达任何管理端模块。
  const reachableFromPortal = collectReachable(manifest, PORTAL_ENTRY_KEY);
  const leaked = [...reachableFromPortal].filter(isAdminOnlyKey);
  if (leaked.length > 0) {
    console.error(`[check-isolation] FAIL: 使用端(${PORTAL_ENTRY_KEY}) 构建产物的依赖图里` +
      "包含管理端专属模块：");
    for (const key of leaked) console.error("  - " + key);
    console.error(`manifest: ${manifestPath}`);
    process.exit(1);
  }

  console.log("[check-isolation] OK");
  console.log(`  manifest: ${manifestPath}`);
  console.log(`  使用端(${PORTAL_ENTRY_KEY}) 可达模块数: ${reachableFromPortal.size}`);
  console.log(`  已确认排除的管理端专属模块 (${ADMIN_ONLY_VIEW_KEYS.length}): ` +
    ADMIN_ONLY_VIEW_KEYS.join(", "));
}

main();
