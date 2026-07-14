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

// ---- 主流厂商预设（M5-2）----
// 只收 OpenAI 兼容端点的厂商（后端 openai-compat 插件通吃）；model 是建议
// 起点，用户可改。Anthropic/Gemini 原生 API 非 OpenAI 兼容，不在此列。

export interface ProviderPreset {
  key: string;
  label: string;
  base_url: string;
  /** 建议模型（预填第一个，可编辑） */
  models: string[];
  /** 建议的密钥环境变量名（用户也可页面直配 api_key，二选一） */
  api_key_env: string;
}

export const PROVIDER_PRESETS: ProviderPreset[] = [
  { key: "dashscope", label: "通义千问（阿里云 DashScope）",
    base_url: "https://dashscope.aliyuncs.com/compatible-mode/v1",
    models: ["qwen-plus", "qwen-max", "qwen-turbo"], api_key_env: "DASHSCOPE_API_KEY" },
  { key: "deepseek", label: "DeepSeek",
    base_url: "https://api.deepseek.com/v1",
    models: ["deepseek-chat", "deepseek-reasoner"], api_key_env: "DEEPSEEK_API_KEY" },
  { key: "zhipu", label: "智谱 GLM",
    base_url: "https://open.bigmodel.cn/api/paas/v4",
    models: ["glm-4.6", "glm-4-flash"], api_key_env: "ZHIPU_API_KEY" },
  { key: "moonshot", label: "月之暗面 Kimi",
    base_url: "https://api.moonshot.cn/v1",
    models: ["kimi-k2-turbo-preview"], api_key_env: "MOONSHOT_API_KEY" },
  { key: "siliconflow", label: "硅基流动 SiliconFlow",
    base_url: "https://api.siliconflow.cn/v1",
    models: ["Qwen/Qwen3-32B", "deepseek-ai/DeepSeek-V3"], api_key_env: "SILICONFLOW_API_KEY" },
  { key: "openai", label: "OpenAI",
    base_url: "https://api.openai.com/v1",
    models: ["gpt-4o-mini", "gpt-4o"], api_key_env: "OPENAI_API_KEY" },
];

/** Provider 表单提交体构建（纯函数，直接单测）。
 * 密钥字段规则：创建时 api_key 非空才带上；编辑时"留空=不动"（不带字段），
 * clearKey=true 显式清除（带 api_key: ""，后端置 NULL 回退环境变量）。 */
export interface ProviderFormShape {
  base_url: string;
  api_key_env: string;
  api_key: string;
  model: string;
  max_concurrency: number;
  params: Record<string, unknown>;
}

export function buildProviderBody(
  form: ProviderFormShape,
  opts: { editing: boolean; clearKey?: boolean },
): Record<string, unknown> {
  const body: Record<string, unknown> = {
    base_url: form.base_url.trim(),
    api_key_env: form.api_key_env.trim(),
    model: form.model.trim(),
    max_concurrency: form.max_concurrency,
    params: form.params,
  };
  const typed = form.api_key.trim();
  if (opts.editing) {
    if (opts.clearKey) body.api_key = "";
    else if (typed) body.api_key = typed;
    // 留空且未勾清除：不带 api_key 字段（后端 exclude_unset 不动它）
  } else if (typed) {
    body.api_key = typed;
  }
  return body;
}

/** 卡片"密钥"行文案：直配 > 环境变量 > 未配置。 */
export function keySourceLabel(p: { has_api_key: boolean; api_key_hint: string | null; api_key_env: string }): string {
  if (p.has_api_key) return `已配置 ${p.api_key_hint ?? "****"}`;
  if (p.api_key_env) return `环境变量 ${p.api_key_env}`;
  return "未配置";
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
