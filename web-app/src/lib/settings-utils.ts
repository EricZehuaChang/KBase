// src/lib/settings-utils.ts —— 设置页纯函数（可测，不依赖 DOM/组件实例）。

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
