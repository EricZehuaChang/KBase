// src/lib/api.ts —— 全端点 typed 客户端。声明式代码，不单测（由使用它的
// 组件测试间接覆盖）。queryConv/queryKb 返回原始 Response，交给调用方用
// parseSSE(reader, handler) 消费流（citations→token*→done）。
import { ref } from "vue";

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

// 401 拦截钩子：router 守卫在启动时注册一个回调（跳转 /login），api.ts 不
// 直接 import router——避免 api.ts ↔ router.ts 循环依赖。未注册时（如单测）
// 拦截器仅清缓存、不做跳转。登录接口本身的 401（用户名/密码错）不走这条
// 拦截逻辑，由 LoginView 自己捕获展示错误——见 login() 用 skipAuthRedirect。
let onUnauthorized: (() => void) | null = null;

export function setUnauthorizedHandler(handler: (() => void) | null): void {
  onUnauthorized = handler;
}

async function req<T>(path: string, init?: RequestInit, opts?: { skipAuthRedirect?: boolean }): Promise<T> {
  const res = await fetch(path, { credentials: "include", ...init });
  if (!res.ok) {
    if (res.status === 401 && !opts?.skipAuthRedirect) {
      clearSessionCache();
      onUnauthorized?.();
    }
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
// credentials: "include" 让会话 Cookie 随请求发出——鉴权开启后这两个端点
// 也需要走 Cookie 会话，和 req() 保持一致。
export function queryConv(convId: string, body: QueryBody, signal?: AbortSignal): Promise<Response> {
  return fetch(`/api/conversations/${convId}/query`, { ...jsonInit(body), credentials: "include", signal });
}

export function queryKb(kbId: string, body: QueryBody, signal?: AbortSignal): Promise<Response> {
  return fetch(`/api/kb/${kbId}/query`, { ...jsonInit(body), credentials: "include", signal });
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

// ---- 认证（M4-1 G5）----

export interface Me {
  username: string;
  role: string;
}

export function login(username: string, password: string): Promise<Me> {
  // skipAuthRedirect：登录失败也是 401，但那是"密码错"不是"会话过期"，不该
  // 触发全局跳转 /login（本来就在 /login 页）——由 LoginView 捕获异常自己展示。
  return req("/api/auth/login", jsonInit({ username, password }), { skipAuthRedirect: true });
}

export async function logout(): Promise<{ ok: boolean }> {
  const r = await req<{ ok: boolean }>("/api/auth/logout", { method: "POST" });
  clearSessionCache();
  return r;
}

export function me(): Promise<Me> {
  return req("/api/auth/me");
}

// 会话探测结果模块级缓存：路由守卫每次导航都要确认"有没有会话"，但不该每次
// 都打一发 /api/auth/me——同一个会话生命周期内缓存结果，登出/401 时清空
// （见 setUnauthorizedHandler 拦截器与 clearSessionCache 调用点）。
let sessionCache: Promise<Me | null> | null = null;

// 全局响应式当前角色（M4-1 G6 角色门控用）：AppShell/各 View 据此隐藏入口。
// 后端已用 require_role 强制校验，这是纯 UX 防呆；未登录或探测失败时为 null，
// 门控函数（canManageContent/canAdminister）对 null 视同无权限。
export const currentRole = ref<string | null>(null);

/** 探测当前会话：命中缓存直接返回；未命中时调用 /api/auth/me 并缓存结果
 * （含"确认无会话"的 null，避免连续未登录状态下的重复探测请求）。 */
export function getSession(): Promise<Me | null> {
  if (sessionCache === null) {
    sessionCache = me().catch(() => null);
    sessionCache.then((session) => {
      currentRole.value = session?.role ?? null;
    });
  }
  return sessionCache;
}

/** 清空会话缓存：401 拦截器与 logout() 成功后调用，强制下次 getSession()
 * 重新探测。同时清空 currentRole——门控 UI 应立即隐藏而不是等下次探测。 */
export function clearSessionCache(): void {
  sessionCache = null;
  currentRole.value = null;
}

// ---- 用户管理（M4-1 G6，admin）----

export interface UserItem {
  id: string;
  username: string;
  role: string;
  disabled: boolean;
  created_at: string;
}

export interface UserCreateBody {
  username: string;
  role: string;
  password: string;
}

export interface UserUpdateBody {
  role?: string;
  disabled?: boolean;
  password?: string;
}

export function listUsers(): Promise<UserItem[]> {
  return req("/api/users");
}

export function createUser(body: UserCreateBody): Promise<UserItem> {
  return req("/api/users", jsonInit(body));
}

export function updateUser(id: string, body: UserUpdateBody): Promise<UserItem> {
  return req(`/api/users/${id}`, jsonInit(body, "PUT"));
}

// ---- API Key 管理（M4-1 G6，admin）----

export interface ApiKeyItem {
  id: string;
  name: string;
  prefix: string;
  role: string;
  revoked: boolean;
  created_at: string;
}

export interface ApiKeyCreateBody {
  name: string;
  role: string;
}

export interface ApiKeyCreated extends ApiKeyItem {
  key: string; // 完整 key，仅创建时返回一次
}

export function listApiKeys(): Promise<ApiKeyItem[]> {
  return req("/api/settings/api-keys");
}

export function createApiKey(body: ApiKeyCreateBody): Promise<ApiKeyCreated> {
  return req("/api/settings/api-keys", jsonInit(body));
}

export function revokeApiKey(id: string): Promise<{ ok: boolean }> {
  return req(`/api/settings/api-keys/${id}`, { method: "DELETE" });
}

// ---- 许可证（M4-1 G6）----

export type LicenseStatus = "trial" | "valid" | "expired" | "invalid";

export interface LicenseInfo {
  status: LicenseStatus;
  org?: string;
  expires?: string;
}

export function getLicense(): Promise<LicenseInfo> {
  return req("/api/license");
}
