// i18n 运行时（方案 A：代码基线 + DB 覆盖合并）。两个 SPA（portal/admin）
// bundle 隔离，各自 main.ts 用同一份工厂配置创建实例并各拉一次 DB 覆盖，
// 保证语言/回落策略一致（见 spec §12 风险条目）。
import { createI18n } from "vue-i18n";

import en from "./locales/en.json";
import ms from "./locales/ms.json";
import zh from "./locales/zh.json";
import { DEFAULT_LANG, SUPPORTED_CODES } from "./languages";

const STORAGE_KEY = "kbase-lang";

// 语言判定优先级：账号偏好 > localStorage 记忆 > 浏览器语言前缀 > 默认（zh）。
// 账号偏好登录后才可知（异步 /api/auth/me），故应用启动时以无账号形式求值
// （localStorage/浏览器）；登录后两 Shell 调 setLanguage(me.language) 应用账号
// 偏好覆盖本地检测（并落 localStorage，下次启动即命中）。免登录分享页无账号，
// 走 localStorage/浏览器，终端用户零配置即得母语界面。
export function detectLanguage(accountLang?: string | null): string {
  if (accountLang && SUPPORTED_CODES.includes(accountLang)) return accountLang;
  const saved = localStorage.getItem(STORAGE_KEY);
  if (saved && SUPPORTED_CODES.includes(saved)) return saved;
  const prefix = (navigator.language || "").slice(0, 2).toLowerCase();
  if (SUPPORTED_CODES.includes(prefix)) return prefix;
  return DEFAULT_LANG;
}

// 账号级语言回写器（P2-4）：登录态下用户手动切语言要同步存到账号（跨设备
// 一致）。i18n 层不直接 import api——core.ts 已 `import { i18n }`，反向 import
// 会形成模块循环——改由 api 层加载时注入回写函数（单向数据依赖）。未注入
// （单测）或未登录时静默跳过。
let accountLanguagePersister: ((lang: string) => void) | null = null;

export function setAccountLanguagePersister(
  fn: ((lang: string) => void) | null,
): void {
  accountLanguagePersister = fn;
}

// 基线 messages 用嵌套结构（json 可读）；DB 覆盖是扁平点分 key，合并前先
// 还原成嵌套，否则 vue-i18n 会把 "kb.create" 当顶层 key 而非嵌套路径。
function unflatten(flat: Record<string, string>): Record<string, unknown> {
  const out: Record<string, unknown> = {};
  for (const [key, value] of Object.entries(flat)) {
    const parts = key.split(".");
    let cur = out;
    for (let i = 0; i < parts.length - 1; i++) {
      cur = (cur[parts[i]] ??= {}) as Record<string, unknown>;
    }
    cur[parts[parts.length - 1]] = value;
  }
  return out;
}

export const i18n = createI18n({
  legacy: false, // Composition API 模式（组件里用 useI18n）
  locale: detectLanguage(),
  fallbackLocale: DEFAULT_LANG, // 缺译文回落中文（全量源，永不裸 key）
  messages: { zh, en, ms },
  missingWarn: false, // fallback 已兜底，不刷缺 key 警告污染控制台
  fallbackWarn: false,
});

// DB 覆盖合并：拉运营在管理端改过的译文，mergeLocaleMessage 覆盖基线
// （DB 优先）。失败静默——打包基线已可渲染，不阻塞（离线兜底设计）。
export async function loadDbOverrides(lang: string): Promise<void> {
  try {
    const res = await fetch(`/api/i18n/${lang}`, { credentials: "same-origin" });
    if (!res.ok) return;
    const overrides = (await res.json()) as Record<string, string>;
    if (overrides && typeof overrides === "object") {
      i18n.global.mergeLocaleMessage(lang, unflatten(overrides));
    }
  } catch {
    // 网络/端点不可用：用打包基线，不影响界面可用
  }
}

// 运行时切语言：更新 vue-i18n locale + 持久化 localStorage + 同步 <html lang>
// + 拉该语言 DB 覆盖（首次切到某语言时把运营改动补上）。persistAccount 缺省
// true——用户手动切换（LanguagePicker）在登录态下回写账号（跨设备一致）；两
// Shell 登录后"应用账号偏好"传 false，避免把刚读到的偏好又原样 PUT 回去。
export async function setLanguage(
  lang: string,
  opts?: { persistAccount?: boolean },
): Promise<void> {
  if (!SUPPORTED_CODES.includes(lang)) return;
  // lang 已过 SUPPORTED_CODES 校验；cast 跟随 vue-i18n 从 messages 推断的
  // locale 联合类型（加语言时自动扩展，不硬编码具体语言码）
  i18n.global.locale.value = lang as typeof i18n.global.locale.value;
  localStorage.setItem(STORAGE_KEY, lang);
  document.documentElement.setAttribute("lang", lang);
  // 账号回写（登录态才实际发生，门槛在注入的 persister 里）；放在拉覆盖前
  // 触发，本身 fire-and-forget 不阻塞切换。
  if (opts?.persistAccount !== false) accountLanguagePersister?.(lang);
  await loadDbOverrides(lang);
}

// 应用启动调用：设初始 <html lang> 并异步拉当前语言的 DB 覆盖。
export function initI18n(): void {
  document.documentElement.setAttribute("lang", i18n.global.locale.value);
  void loadDbOverrides(i18n.global.locale.value);
}
