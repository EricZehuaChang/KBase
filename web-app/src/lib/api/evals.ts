// lib/api/evals.ts —— 评测回归域（B）：评测集 CRUD、一键回归
// （hit@k/MRR）、历史与逐用例明细；T15 追加答案级评测（mode="answer"，
// 走 eval_answer 长任务）与回归报告导出（md/docx）。
import { jsonInit, req } from "./core";
import type { Job } from "./jobs";

export interface EvalCase {
  question: string;
  expect_doc?: string;
  expect_text?: string;
  // T13 回灌的参考答案：只带它的用例不进 hit@k/MRR 分母，但**参与答案级判分**
  // （T15）——裁判拿它当评分基准。
  expected_answer?: string;
}

export interface EvalSetItem {
  id: string;
  name: string;
  case_count: number;
  created_at: string;
}

// "retrieval"=只评检索（默认，同步秒级返回、零 LLM 调用）；
// "answer"=检索+生成+裁判打分（须服务端开 evals.answer_judge.enabled，否则 422）。
export type EvalMode = "retrieval" | "answer";

export interface EvalRunDetail {
  question: string;
  rank: number | null;
  hit: boolean | null;
  top_doc: string | null;
  expected_answer?: string | null;
  // 该用例是否参与检索判分（T13 只带回灌参考答案的用例为 false）
  retrieval_judged?: boolean;
  // 答案级判分（T15）：answer 是本用例生成的答案，answer_score 为 0~1 裁判分，
  // answer_reason 是一句话理由，judge_error 非空表示该用例没判出来
  // （no_reference=用例没给参考答案，judge_failed=裁判调用/输出异常）。
  answer?: string;
  answer_score?: number | null;
  answer_reason?: string | null;
  judge_provider?: string | null;
  judge_error?: string | null;
}

export interface EvalRunResult {
  id: string;
  set_id?: string;
  mode?: EvalMode;
  top_k: number;
  hit_rate: number;
  mrr: number;
  total: number;
  hits?: number;
  skipped?: number;
  case_count?: number;
  answer_score?: number | null;
  judge_provider?: string | null;
  judged_count?: number;
  judge_failed_count?: number;
  created_at: string;
  details?: EvalRunDetail[];
}

// mode="answer" 的返回体：不是 run 结果，而是待轮询的 job。
export interface EvalAnswerJob {
  id: string;
  mode: EvalMode;
  status: string;
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

// 检索回归（默认）：同步返回本次 run 的结果，行为与改造前一致（零 LLM 调用）。
export function runEvalSet(setId: string, topK = 5): Promise<EvalRunResult> {
  return req(`/api/eval-sets/${setId}/run`, jsonInit({ top_k: topK }));
}

// 答案级回归（T15）：只建任务、立刻返回 job id（判分分钟级，不能同步等）。
// 服务端未开开关时抛 422（error.answer_judge_disabled）——不静默退回检索回归。
export function runEvalSetAnswer(
  setId: string,
  topK = 5,
  provider?: string | null,
): Promise<EvalAnswerJob> {
  return req(`/api/eval-sets/${setId}/run`, jsonInit({
    top_k: topK, mode: "answer", provider: provider ?? undefined,
  }));
}

export function listEvalRuns(setId: string): Promise<EvalRunResult[]> {
  return req(`/api/eval-sets/${setId}/runs`);
}

export function getEvalRun(runId: string): Promise<EvalRunResult & { set_id: string }> {
  return req(`/api/eval-runs/${runId}`);
}

// 报告直链（浏览器原生下载，不经 fetch+blob，与 jobs 的 artifactUrl 同模式）。
export function evalReportUrl(runId: string, format: "md" | "docx"): string {
  return `/api/eval-runs/${runId}/report?format=${format}`;
}

// 报告 md 正文（前端预览用；报告含用例数/检索指标/答案分/裁判模型/复测口径）。
export async function fetchEvalReport(runId: string): Promise<string> {
  const res = await fetch(evalReportUrl(runId, "md"));
  if (!res.ok) throw new Error(`报告加载失败（HTTP ${res.status}）`);
  return res.text();
}

// 答案级任务的进度详情（复用 jobs 域的类型：同一套 progress.steps 结构）。
export type EvalAnswerJobDetail = Job;
