// src/lib/settings-utils.ts —— 设置页纯函数与共享类型（可测，不依赖 DOM/组件实例）。

/** 单个 provider 的连通性测试状态（SettingsView 按 provider 名持有，
 * ProviderCard 据此渲染 spinner / 绿延迟徽章 / 红失败 tooltip）。 */
export interface ProviderTestState {
  status: "idle" | "testing" | "ok" | "fail";
  latencyMs?: number;
  error?: string;
}

/** params JSON 校验结果：ok 时 value 为解析后的对象；失败时 error 为中文提示，
 * 供 Dialog 表单在 textarea 下方内联展示。 */
export type ParamsValidation =
  | { ok: true; value: Record<string, unknown> }
  | { ok: false; error: string };

/** 校验 params 文本框内容：空字符串视为 {}（允许不填）；非法 JSON 或 JSON
 * 顶层不是对象（如数组/字符串/数字）均报错——params 语义上必须是键值对。 */
export function validateParamsJson(text: string): ParamsValidation {
  const trimmed = text.trim();
  if (trimmed === "") return { ok: true, value: {} };
  let parsed: unknown;
  try {
    parsed = JSON.parse(trimmed);
  } catch (err) {
    return { ok: false, error: `JSON 格式错误：${err instanceof Error ? err.message : String(err)}` };
  }
  if (parsed === null || typeof parsed !== "object" || Array.isArray(parsed)) {
    return { ok: false, error: "params 必须是 JSON 对象（如 {\"temperature\":0.7}）" };
  }
  return { ok: true, value: parsed as Record<string, unknown> };
}

/** params 对象 → 卡片摘要文本（如 "temperature=0.7, top_p=0.9"）；空对象返回
 * 占位符 "—" 避免卡片出现空白行。 */
export function paramsSummary(params: Record<string, unknown> | null | undefined): string {
  if (!params) return "—";
  const entries = Object.entries(params);
  if (entries.length === 0) return "—";
  return entries.map(([k, v]) => `${k}=${JSON.stringify(v)}`).join(", ");
}

/** healthz 各组件状态 → 状态点语义色 class。reranker 字段用 on/off/degraded；
 * embedder/vectorstore 字段是类名字符串，非空即视为 ok。未知值兜底灰色。 */
export interface StatusDotInfo {
  label: string;
  class: string;
}

export function healthDot(value: string): StatusDotInfo {
  if (value === "ok" || value === "on") return { label: value, class: "bg-[var(--ok)]" };
  if (value === "degraded") return { label: value, class: "bg-[var(--warn)]" };
  if (value === "off") return { label: value, class: "bg-[var(--text-3)]" };
  // embedder/vectorstore 是类名（如 "LocalEmbedder"），非空字符串即正常
  return value ? { label: value, class: "bg-[var(--ok)]" } : { label: "—", class: "bg-[var(--text-3)]" };
}

// ---- 许可证横幅（M4-1 G6）----

/** 与 api.ts LicenseInfo 结构一致但在此模块独立声明——纯函数不依赖 api.ts，
 * 避免 settings-utils（vitest 直测）牵连 fetch 相关的模块副作用。 */
export interface LicenseLike {
  status: "trial" | "valid" | "expired" | "invalid";
  org?: string;
  expires?: string;
}

export interface LicenseBannerInfo {
  tone: "info" | "warn";
  message: string;
}

/** valid 状态不展示横幅（返回 null）；trial 用提示色，expired/invalid 用警告色
 * （spec：不锁功能，只提示）。AppShell 顶部细条横幅据此渲染文案与配色。 */
export function licenseBannerInfo(license: LicenseLike): LicenseBannerInfo | null {
  if (license.status === "valid") return null;
  if (license.status === "trial") {
    return { tone: "info", message: "当前为试用模式，功能不受限，建议尽快导入正式许可证" };
  }
  if (license.status === "expired") {
    return {
      tone: "warn",
      message: `许可证已过期（${license.expires ?? "未知日期"}），请联系管理员更新`,
    };
  }
  return { tone: "warn", message: "许可证无效，请检查 license.json 是否被篡改或损坏" };
}

// ---- 用户管理（M4-1 G6）----

/** 与 api.ts UserItem 结构一致的最小形状——纯函数独立声明，理由同 LicenseLike。 */
export interface UserLike {
  id: string;
  role: string;
  disabled: boolean;
}

/** 客户端镜像后端"不能禁用/降级最后一个管理员"的不变量，用于前置禁用相关
 * 操作按钮（真正的强制仍在后端，见 kbase/api/main.py update_user）。
 * 判定：目标用户是启用中的 admin，且没有其他启用中的 admin 在场。 */
export function isLastEnabledAdmin(users: UserLike[], userId: string): boolean {
  const target = users.find((u) => u.id === userId);
  if (!target || target.role !== "admin" || target.disabled) return false;
  const otherEnabledAdmins = users.filter(
    (u) => u.id !== userId && u.role === "admin" && !u.disabled,
  );
  return otherEnabledAdmins.length === 0;
}
