// lib/api/jobs.ts —— 生成任务域：方案大纲、长任务（proposal/digest）
// 创建/查询与产物直链。
import { jsonInit, req } from "./core";

export interface OutlineSection {
  title: string;
  brief: string;
}

export type JobType = "proposal" | "digest";
export type JobStatus = "pending" | "running" | "done" | "done_with_errors" | "failed";
export type JobStepStatus = "pending" | "running" | "done" | "failed";

export interface JobStep {
  name: string;
  status: JobStepStatus;
  detail?: string | null;
}

export interface JobProgress {
  steps: JobStep[];
}

export interface Job {
  id: string;
  kb_id: string;
  type: JobType;
  status: JobStatus;
  params: Record<string, unknown> | null;
  progress: JobProgress | null;
  artifact_path: string | null;
  error: string | null;
  provider: string | null;
  created_at: string;
  updated_at: string;
}

export interface JobCreateBody {
  type: JobType;
  kb_id: string;
  provider?: string | null;
  params: Record<string, unknown>;
}

export function generateOutline(
  kbId: string,
  topic: string,
  requirements: string,
  provider?: string | null,
): Promise<OutlineSection[]> {
  return req("/api/proposals/outline", jsonInit({
    kb_id: kbId, topic, requirements, provider: provider ?? undefined,
  }));
}

export function createJob(body: JobCreateBody): Promise<{ id: string }> {
  return req("/api/jobs", jsonInit(body));
}

export function listJobs(kbId: string): Promise<Job[]> {
  return req(`/api/jobs?kb_id=${encodeURIComponent(kbId)}`);
}

export function getJob(id: string): Promise<Job> {
  return req(`/api/jobs/${id}`);
}

// 直链：md 用于 <pre> 预览 fetch，docx 用于下载按钮 href（浏览器原生下载，
// 不经 fetch+blob）。
export function artifactUrl(id: string, format: "md" | "docx"): string {
  return `/api/jobs/${id}/artifact?format=${format}`;
}
