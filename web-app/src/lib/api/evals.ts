// lib/api/evals.ts —— 评测回归域（B）：评测集 CRUD、一键回归
// （hit@k/MRR）、历史与逐用例明细。
import { jsonInit, req } from "./core";

export interface EvalCase {
  question: string;
  expect_doc?: string;
  expect_text?: string;
}

export interface EvalSetItem {
  id: string;
  name: string;
  case_count: number;
  created_at: string;
}

export interface EvalRunResult {
  id: string;
  top_k: number;
  hit_rate: number;
  mrr: number;
  total: number;
  hits?: number;
  created_at: string;
  details?: { question: string; rank: number | null; hit: boolean; top_doc: string | null }[];
}

export function listEvalSets(kbId: string): Promise<EvalSetItem[]> {
  return req(`/api/kb/${kbId}/eval-sets`);
}

export function createEvalSet(kbId: string, name: string, cases: EvalCase[]): Promise<EvalSetItem> {
  return req(`/api/kb/${kbId}/eval-sets`, jsonInit({ name, cases }));
}

export function deleteEvalSet(setId: string): Promise<{ ok: boolean }> {
  return req(`/api/eval-sets/${setId}`, { method: "DELETE" });
}

export function runEvalSet(setId: string, topK = 5): Promise<EvalRunResult> {
  return req(`/api/eval-sets/${setId}/run`, jsonInit({ top_k: topK }));
}

export function listEvalRuns(setId: string): Promise<EvalRunResult[]> {
  return req(`/api/eval-sets/${setId}/runs`);
}

export function getEvalRun(runId: string): Promise<EvalRunResult & { set_id: string }> {
  return req(`/api/eval-runs/${runId}`);
}
