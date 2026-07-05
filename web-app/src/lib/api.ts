// src/lib/api.ts —— 全端点 typed 客户端。声明式代码，不单测（由使用它的
// 组件测试间接覆盖）。queryConv/queryKb 返回原始 Response，交给调用方用
// parseSSE(reader, handler) 消费流（citations→token*→done）。

export interface EnrichConfig {
  enabled: boolean;
}

export interface KbConfig {
  chunk_size?: number;
  chunk_overlap?: number;
  enrich?: EnrichConfig;
}

export interface Kb {
  id: string;
  name: string;
  config: KbConfig | null;
}

export interface DocumentItem {
  id: string;
  filename: string;
  status: string; // pending | parsing | pending_ocr | ready | failed
  error: string | null;
}

export interface DocumentContent {
  doc_id: string;
  filename: string;
  markdown: string;
}

export interface Citation {
  index: number;
  // 可选：M2 中期才加入 citations 载荷。会话历史里旧助手消息的 citations JSON
  // 没有该字段，渲染层须优雅降级（无 doc_id 时隐藏"查看文档全文"）。
  doc_id?: string;
  doc_name: string;
  heading_path: string;
  snippet: string;
  score: number;
}

export interface ContextBlock {
  doc_id: string;
  doc_name: string;
  heading_path: string;
  text: string;
  snippet: string;
  score: number;
}

export interface TraceStage {
  // [chunk_id, score][]
  [stage: string]: [string, number][];
}

export interface SearchResult {
  blocks: ContextBlock[];
  trace?: TraceStage;
}

export interface Conversation {
  id: string;
  kb_id: string;
  title: string | null;
  updated_at?: string;
}

export interface ConversationPage {
  items: Conversation[];
  total: number;
}

export interface Message {
  id: string;
  role: "user" | "assistant";
  content: string;
  citations: string | null; // JSON 字符串，assistant 消息才有
  provider: string | null;
}

export interface Provider {
  name: string;
  base_url: string;
  api_key_env: string;
  model: string;
  max_concurrency: number;
  params: Record<string, unknown>;
}

export interface ProviderCreateBody {
  name: string;
  base_url: string;
  api_key_env: string;
  model: string;
  max_concurrency?: number;
  params?: Record<string, unknown>;
}

export interface ProviderUpdateBody {
  base_url?: string;
  api_key_env?: string;
  model?: string;
  max_concurrency?: number;
  params?: Record<string, unknown>;
}

export interface ProvidersResponse {
  active: string | null;
  providers: Provider[];
}

export interface ProviderTestResult {
  ok: boolean;
  latency_ms?: number;
  error?: string;
}

export interface QueryBody {
  question: string;
  provider?: string | null;
  top_k?: number;
}

export interface HealthzResponse {
  status: string;
  embedder: string;
  vectorstore: string;
  reranker: "on" | "off" | "degraded";
}

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

async function req<T>(path: string, init?: RequestInit): Promise<T> {
  const res = await fetch(path, init);
  if (!res.ok) {
    const text = await res.text();
    let detail: string | undefined;
    try {
      detail = JSON.parse(text)?.detail;
    } catch {
      // 非 JSON 响应体，原样用 text 兜底
    }
    // 兜底状态码文案：网关错误（如 502）响应体可能为空，空字符串消息的 Error
    // 在调用方 v-if 判断中是假值，会让错误态"消失"，统一在这里保证消息非空。
    throw new Error(detail || text || `请求失败 (${res.status})`);
  }
  if (res.status === 204) return undefined as T;
  return res.json() as Promise<T>;
}

function jsonInit(body: unknown, method = "POST"): RequestInit {
  return {
    method,
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify(body),
  };
}

export function listKbs(): Promise<Kb[]> {
  return req("/api/kb");
}

export function createKb(name: string): Promise<Kb> {
  return req("/api/kb", jsonInit({ name }));
}

export function deleteKb(kbId: string): Promise<{ ok: boolean }> {
  return req(`/api/kb/${kbId}`, { method: "DELETE" });
}

export function putKbConfig(kbId: string, config: KbConfig): Promise<{ ok: boolean }> {
  return req(`/api/kb/${kbId}/config`, jsonInit(config, "PUT"));
}

export function listDocs(kbId: string): Promise<DocumentItem[]> {
  return req(`/api/kb/${kbId}/documents`);
}

export function uploadDocs(kbId: string, files: FormData): Promise<{ accepted: string[] }> {
  return req(`/api/kb/${kbId}/documents`, { method: "POST", body: files });
}

export function deleteDoc(kbId: string, docId: string): Promise<{ ok: boolean }> {
  return req(`/api/kb/${kbId}/documents/${docId}`, { method: "DELETE" });
}

export function retryDoc(docId: string): Promise<{ id: string; status: string; error: string | null }> {
  return req(`/api/documents/${docId}/retry`, { method: "POST" });
}

export function retryOcr(kbId: string): Promise<{ retrying: string[] }> {
  return req(`/api/kb/${kbId}/retry-ocr`, { method: "POST" });
}

export function getDocContent(docId: string): Promise<DocumentContent> {
  return req(`/api/documents/${docId}/content`);
}

export function search(
  kbId: string,
  query: string,
  opts?: { topK?: number; debug?: boolean },
): Promise<SearchResult> {
  return req(`/api/kb/${kbId}/search`, jsonInit({
    query, top_k: opts?.topK ?? 5, debug: opts?.debug ?? false,
  }));
}

export function listConvs(opts?: {
  kbId?: string;
  limit?: number;
  offset?: number;
}): Promise<ConversationPage> {
  const params = new URLSearchParams();
  if (opts?.kbId) params.set("kb_id", opts.kbId);
  if (opts?.limit !== undefined) params.set("limit", String(opts.limit));
  if (opts?.offset !== undefined) params.set("offset", String(opts.offset));
  const qs = params.toString();
  return req(`/api/conversations${qs ? `?${qs}` : ""}`);
}

export function createConv(kbId: string): Promise<Conversation> {
  return req("/api/conversations", jsonInit({ kb_id: kbId }));
}

export function listMessages(convId: string): Promise<Message[]> {
  return req(`/api/conversations/${convId}/messages`);
}

// SSE 端点：返回原始 Response，调用方自己取 reader 喂给 parseSSE。
// signal 用于中途取消（切换会话/知识库或离开页面时 abort，避免旧流继续写入）。
export function queryConv(convId: string, body: QueryBody, signal?: AbortSignal): Promise<Response> {
  return fetch(`/api/conversations/${convId}/query`, { ...jsonInit(body), signal });
}

export function queryKb(kbId: string, body: QueryBody, signal?: AbortSignal): Promise<Response> {
  return fetch(`/api/kb/${kbId}/query`, { ...jsonInit(body), signal });
}

export function listProviders(): Promise<{ active: string | null; providers: string[] }> {
  return req("/api/providers");
}

export function settingsListProviders(): Promise<ProvidersResponse> {
  return req("/api/settings/providers");
}

export function createProvider(body: ProviderCreateBody): Promise<{ ok: boolean }> {
  return req("/api/settings/providers", jsonInit(body));
}

export function updateProvider(name: string, body: ProviderUpdateBody): Promise<{ ok: boolean }> {
  return req(`/api/settings/providers/${name}`, jsonInit(body, "PUT"));
}

export function deleteProvider(name: string): Promise<{ ok: boolean }> {
  return req(`/api/settings/providers/${name}`, { method: "DELETE" });
}

export function setActiveProvider(name: string): Promise<{ ok: boolean }> {
  return req("/api/settings/active-provider", jsonInit({ name }, "PUT"));
}

export function testProvider(name: string): Promise<ProviderTestResult> {
  return req(`/api/settings/providers/${name}/test`, { method: "POST" });
}

export function healthz(): Promise<HealthzResponse> {
  return req("/healthz");
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
