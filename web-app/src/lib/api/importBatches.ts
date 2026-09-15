// lib/api/importBatches.ts —— T18 批量导入批次域：只读的清单 / 明细 / CSV 导出。
//
// 这里**只有读**：触发导入的入口刻意只有命令行（python -m kbase.bulk_import），
// 网页上传走 /api/kb/{id}/documents，两者互不替代（理由见
// kbase/bulk_import.py 的文件头与 kbase/api/routes/import_batches.py 的注释）。
import { req } from "./core";

/** 批次终态：running=进行中；done=全部成功；done_with_errors=跑完但有失败文件
 * （可 --retry-failed 重跑）；failed=一个都没成功；interrupted=被中断/进程没
 * 正常结束（可续传）。取值与后端 bulk_import.py 的模块常量一致。 */
export type ImportBatchStatus =
  | "running" | "done" | "done_with_errors" | "failed" | "interrupted";

/** 批次汇总（后端 counts JSON 的投影；运行中只有 total，跑完才完整）。 */
export interface ImportBatchSummary {
  status?: ImportBatchStatus;
  /** 本轮待处理的文件数（清单里已有条目 + 待处理） */
  total?: number;
  pending?: number;
  done?: number;
  failed?: number;
  elapsed_s?: number | null;
  workers?: number;
  parse_mode?: string;
  /** 导入的根目录（给人看的排查线索） */
  dir?: string;
  /** 被中断时 true */
  interrupted?: boolean;
  error?: string;
  exit_code?: number;
}

export interface ImportBatch {
  id: string;
  kb_id: string;
  /** 相对 data_dir 的清单路径（CLI --manifest 或默认 import-<kb>.jsonl） */
  manifest_path: string;
  started_by: string | null;
  started_at: string | null;
  finished_at: string | null;
  status: ImportBatchStatus;
  summary: ImportBatchSummary;
  /** 仅单批次详情有：清单文件是否可读（越界/被删时为 false） */
  manifest_available?: boolean;
}

/** 清单里的一个文件条目（逐文件台账，字段与后端 IMPORT_COLUMNS 同源）。 */
export interface ImportEntry {
  path: string;
  status: "done" | "failed" | string;
  size: number | null;
  mtime: number | null;
  doc_id: string | null;
  /** 文档落库后的状态（ready/pending_ocr/pending_review），失败条目为 null */
  doc_status: string | null;
  error: string | null;
  /** 写入清单的时间（UTC ISO） */
  ts: string | null;
}

export interface ImportEntryPage {
  items: ImportEntry[];
  total: number;
  /** 同批次失败条目总数（与 failures_only 过滤无关，供页面显示"1/30 失败"） */
  failures: number;
  manifest_available: boolean;
  batch: ImportBatch | null;
}

/** 批次清单（新→旧）。kbId 给了就按库过滤（后端逐行过 ACL + key scope）。 */
export function listImportBatches(kbId: string, limit = 50): Promise<{ items: ImportBatch[]; total: number }> {
  return req(`/api/import-batches?kb_id=${encodeURIComponent(kbId)}&limit=${limit}`);
}

export function getImportBatch(batchId: string): Promise<ImportBatch> {
  return req(`/api/import-batches/${encodeURIComponent(batchId)}`);
}

/** 批次明细。failuresOnly=true 只看失败（支持工单最常见的那一问）。 */
export function getImportEntries(
  batchId: string,
  opts: { failuresOnly?: boolean; limit?: number; offset?: number } = {},
): Promise<ImportEntryPage> {
  const q = new URLSearchParams();
  if (opts.failuresOnly) q.set("failures_only", "true");
  q.set("limit", String(opts.limit ?? 200));
  q.set("offset", String(opts.offset ?? 0));
  return req(`/api/import-batches/${encodeURIComponent(batchId)}/entries?${q.toString()}`);
}

/** CSV 导出的直链（列完整，含失败原因）。用 <a download> 触发，不走 fetch
 * ——导出是文件下载，交给浏览器处理进度与另存。 */
export function importBatchExportUrl(batchId: string, failuresOnly = false): string {
  const q = failuresOnly ? "?failures_only=true" : "";
  return `/api/import-batches/${encodeURIComponent(batchId)}/export.csv${q}`;
}
