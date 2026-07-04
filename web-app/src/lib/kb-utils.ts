// src/lib/kb-utils.ts —— 知识库页纯函数（可测，不依赖 DOM/组件实例）。
import type { DocumentItem } from "@/lib/api";

/** 文档状态 → Badge 展示信息。variant 对应 badge.vue 的 CVA variant
 * （default 走 accent，这里语义色用内联 class 覆盖，见 StatusBadgeInfo.class）。 */
export interface StatusBadgeInfo {
  label: string;
  /** 语义色 class：直接用设计令牌，不依赖 shadcn 默认配色 */
  class: string;
}

const STATUS_MAP: Record<string, StatusBadgeInfo> = {
  ready: { label: "就绪", class: "bg-[var(--ok-weak)] text-[var(--ok)]" },
  parsing: { label: "解析中", class: "bg-[var(--warn-weak)] text-[var(--warn)]" },
  pending_ocr: { label: "待OCR", class: "bg-[var(--warn-weak)] text-[var(--warn)]" },
  pending: { label: "等待中", class: "bg-[var(--surface-2)] text-[var(--text-2)]" },
  failed: { label: "失败", class: "bg-[var(--err-weak)] text-[var(--err)]" },
};

/** 状态 → Badge 展示信息映射纯函数。未知状态兜底为中性灰色展示原始值，
 * 避免后端新增状态时前端直接崩溃或空白。 */
export function statusBadge(status: string): StatusBadgeInfo {
  return STATUS_MAP[status] ?? { label: status, class: "bg-[var(--surface-2)] text-[var(--text-2)]" };
}

/** 是否存在需要轮询的状态（parsing/pending/pending_ocr）。全部 ready/failed
 * 时返回 false，调用方据此停止 3s 轮询定时器。 */
export function hasPollingStatus(docs: DocumentItem[]): boolean {
  return docs.some((d) => d.status === "parsing" || d.status === "pending" || d.status === "pending_ocr");
}

/** 是否存在 pending_ocr 文档——决定"批量重试OCR"按钮是否显示。 */
export function hasPendingOcr(docs: DocumentItem[]): boolean {
  return docs.some((d) => d.status === "pending_ocr");
}

/** 是否可对该文档发起单条重试：failed 与 pending_ocr 均可重试
 * （pending_ocr 走同一个 retryDoc 接口，等价于对该文档单独触发 OCR 重试）。 */
export function canRetryDoc(status: string): boolean {
  return status === "failed" || status === "pending_ocr";
}
