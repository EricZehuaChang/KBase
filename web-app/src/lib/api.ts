// src/lib/api.ts —— 全端点 typed 客户端。声明式代码，不单测（由使用它的
// 组件测试间接覆盖）。queryConv/queryKb 返回原始 Response，交给调用方用
// parseSSE(reader, handler) 消费流（citations→token*→done）。
import { ref } from "vue";

export interface EnrichConfig {
  enabled: boolean;
}

// M6-1.5 KB 级检索策略：各键缺省/null=跟随全局默认（"通用方式"）
export interface KbRetrievalConfig {
  hybrid?: boolean | null;      // 多路召回（关键词路）
  rerank?: boolean | null;
  rewrite?: "off" | "conditional" | "always" | null;
  candidates?: number | null;
}

export interface KbConfig {
  chunk_size?: number;
  chunk_overlap?: number;
  enrich?: EnrichConfig;
  // M5-2：建库时绑定的向量模型 id（GET /api/embedders 清单）；缺省=默认模型。
  // 只读展示——建库后不可改（换模型=全库向量作废，需重建）。
  embedder?: string;
  retrieval?: KbRetrievalConfig;
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
  // M5-2 引用定位：命中内容在源文件中的页码（文本层 PDF 才有；老消息/其他
  // 格式为 null 或缺失）。预览原文件时用 #page= 跳页。
  page?: number | null;
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
  // M5-2 页面直配密钥：后端脱敏视图——原文永不出站，只回"配没配"与尾4位提示
  has_api_key: boolean;
  api_key_hint: string | null;
}

export interface ProviderCreateBody {
  name: string;
  base_url: string;
  api_key_env?: string;
  api_key?: string;          // 页面直配密钥（与 api_key_env 至少给一个）
  model: string;
  max_concurrency?: number;
  params?: Record<string, unknown>;
}

export interface ProviderUpdateBody {
  base_url?: string;
  api_key_env?: string;
  api_key?: string;          // 缺省=不动；""=清除直配密钥（回退环境变量）
  model?: string;
  max_concurrency?: number;
  params?: Record<string, unknown>;
}

// ---- KB 级向量模型（M5-2）----

export interface EmbedderInfo {
  id: string;                // "default" 或 cfg.embedders 清单里的选项 id
  plugin: string;            // bge-local | tei | openai-embed
  model: string | null;
}

export interface EmbeddersCatalog {
  default: EmbedderInfo;
  options: EmbedderInfo[];
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

/** 统一处理一次 401（会话失效）：清会话缓存并触发注册的跳转回调。返回是否
 * 有回调被触发——调用方（如 SSE 流的手工 fetch 分支）据此决定是否还需展示
 * 兜底错误气泡：有回调时页面已在跳登录页，气泡冗余；无回调（如单测/未注册）
 * 时仍应展示，避免错误无声消失。req() 与 useChat 的 raw-fetch 分支共用本函数，
 * 保证两条 401 路径行为一致。 */
export function handleUnauthorized(): boolean {
  clearSessionCache();
  if (onUnauthorized) {
    onUnauthorized();
    return true;
  }
  return false;
}

async function req<T>(path: string, init?: RequestInit, opts?: { skipAuthRedirect?: boolean }): Promise<T> {
  const res = await fetch(path, { credentials: "include", ...init });
  if (!res.ok) {
    if (res.status === 401 && !opts?.skipAuthRedirect) {
      handleUnauthorized();
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

export function createKb(name: string, embedder?: string): Promise<Kb> {
  // embedder 缺省/"default" 不传字段——后端绑定默认模型，与改造前行为一致
  const body = embedder && embedder !== "default" ? { name, embedder } : { name };
  return req("/api/kb", jsonInit(body));
}

export function listEmbedders(): Promise<EmbeddersCatalog> {
  return req("/api/embedders");
}

// 换绑向量模型（admin 重操作）：全库向量作废，按新模型后台重嵌入重建
export function rebindEmbedder(
  kbId: string, embedder: string,
): Promise<{ ok: boolean; from: string; to: string }> {
  return req(`/api/kb/${kbId}/rebind-embedder`, jsonInit({ embedder }));
}

export function deleteKb(kbId: string): Promise<{ ok: boolean }> {
  return req(`/api/kb/${kbId}`, { method: "DELETE" });
}

// ---- 库级权限（M6-3）：空授权=公开，一配即收紧 ----

export interface KbGrant {
  user_id: string;
  username: string | null;
  created_at: string;
}

export function getKbGrants(kbId: string): Promise<{ grants: KbGrant[] }> {
  return req(`/api/kb/${kbId}/grants`);
}

export function putKbGrants(kbId: string, userIds: string[]): Promise<{ ok: boolean; count: number }> {
  return req(`/api/kb/${kbId}/grants`, jsonInit({ user_ids: userIds }, "PUT"));
}

export function putKbConfig(kbId: string, config: KbConfig): Promise<{ ok: boolean }> {
  return req(`/api/kb/${kbId}/config`, jsonInit(config, "PUT"));
}

export function listDocs(kbId: string): Promise<DocumentItem[]> {
  return req(`/api/kb/${kbId}/documents`);
}

// parseMode（F）："auto"=既有管道；"ocr"=表格增强（文本层 PDF 也强制
// GLM-OCR 结构化解析，跨页断表可合并）；"vlm"=满血视觉模型深度识别
// （仅图片格式生效，识别后停 pending_review 等人工校验确认才向量化）。
export function uploadDocs(
  kbId: string, files: FormData, parseMode: "auto" | "ocr" | "vlm" = "auto",
): Promise<{ accepted: string[] }> {
  if (parseMode !== "auto") files.set("parse_mode", parseMode);
  return req(`/api/kb/${kbId}/documents`, { method: "POST", body: files });
}

// 向量模型密钥页面配置：DB 覆盖 > api_key_env（与 Provider 同规矩）
export interface EmbedderKeyItem {
  id: string;
  plugin: string;
  model: string;
  api_key_env: string;
  has_db_key: boolean;
  key_hint: string | null;
}

export function listEmbedderKeys(): Promise<{ items: EmbedderKeyItem[] }> {
  return req("/api/settings/embedder-keys");
}

export function putEmbedderKey(id: string, apiKey: string): Promise<{ ok: boolean }> {
  return req(`/api/settings/embedder-keys/${id}`, jsonInit({ api_key: apiKey }, "PUT"));
}

export function deleteEmbedderKey(id: string): Promise<{ ok: boolean }> {
  return req(`/api/settings/embedder-keys/${id}`, { method: "DELETE" });
}

// M6-8 企业 SSO：登录页据此显示"企业账号登录"入口
export function getSsoStatus(): Promise<{ enabled: boolean }> {
  return req("/api/auth/sso/status");
}

// M6-7 URL 连接器：拉取网页导入知识库（内网 wiki/门户为主用途）
export function importUrl(kbId: string, url: string): Promise<{ accepted: string[] }> {
  return req(`/api/kb/${kbId}/import-url`, jsonInit({ url }));
}

// M6-4 反馈看板：赞/踩总量 + 差评清单（带问题原文与备注）
export interface FeedbackStats {
  up: number;
  down: number;
  items: {
    message_id: string;
    kb_id: string | null;
    question: string | null;
    answer_excerpt: string;
    note: string | null;
    created_at: string;
  }[];
}

export function getFeedbackStats(limit = 20): Promise<FeedbackStats> {
  return req(`/api/stats/feedback?limit=${limit}`);
}

// M6-4 反馈闭环：对助手消息点赞(1)/点踩(-1)，重复提交覆盖
export function submitFeedback(
  messageId: string, rating: 1 | -1, note?: string,
): Promise<{ message_id: string; rating: number }> {
  return req(`/api/messages/${messageId}/feedback`, jsonInit({ rating, note }));
}

// E POC 演示数据一键装载：幂等——演示库已存在时 created=false 直接返回其 id
export function loadDemoData(): Promise<{ id: string; name: string; created: boolean }> {
  return req("/api/demo-data", { method: "POST" });
}

// E 上传进度：fetch 不暴露上传进度事件，改用 XHR 的 upload.onprogress。
// onProgress 收 0-100 整数百分比（仅指字节送达服务器的进度；后续解析/
// 向量化是异步 job，由文档状态轮询展示）。错误语义与 req() 对齐：401 触发
// 全局登出跳转，其余取 detail 文案。
export function uploadDocsWithProgress(
  kbId: string, files: FormData, parseMode: "auto" | "ocr" | "vlm" = "auto",
  onProgress?: (percent: number) => void,
): Promise<{ accepted: string[] }> {
  if (parseMode !== "auto") files.set("parse_mode", parseMode);
  return new Promise((resolve, reject) => {
    const xhr = new XMLHttpRequest();
    xhr.open("POST", `/api/kb/${kbId}/documents`);
    xhr.upload.onprogress = (e) => {
      if (e.lengthComputable && onProgress) {
        onProgress(Math.round((e.loaded / e.total) * 100));
      }
    };
    xhr.onload = () => {
      if (xhr.status >= 200 && xhr.status < 300) {
        resolve(JSON.parse(xhr.responseText) as { accepted: string[] });
        return;
      }
      if (xhr.status === 401 && handleUnauthorized()) {
        reject(new Error("登录已失效"));
        return;
      }
      let detail: string | undefined;
      try {
        detail = (JSON.parse(xhr.responseText) as { detail?: string }).detail;
      } catch {
        // 非 JSON 响应体，用状态码兜底
      }
      reject(new Error(detail ?? `上传失败（HTTP ${xhr.status}）`));
    };
    xhr.onerror = () => reject(new Error("网络错误，上传失败"));
    xhr.send(files);
  });
}

// F 校验确认入库：markdown 缺省=按识别结果原样；给了=以编辑稿为准
export function reviewDocument(
  docId: string, markdown?: string,
): Promise<{ id: string; status: string; error: string | null }> {
  return req(`/api/documents/${docId}/review`,
             jsonInit({ markdown: markdown ?? null }, "PUT"));
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

// ---- Chunk 运营管理（M6-1）----

export interface ChunkItem {
  id: string;
  doc_id: string;
  heading_path: string;
  text: string;
  is_leaf: boolean;
  page: number | null;
  enabled: boolean;
  chars: number;
}

export interface ChunkPage {
  items: ChunkItem[];
  total: number;
}

export function listDocChunks(
  docId: string,
  opts?: { offset?: number; limit?: number; q?: string },
): Promise<ChunkPage> {
  const params = new URLSearchParams();
  if (opts?.offset) params.set("offset", String(opts.offset));
  if (opts?.limit) params.set("limit", String(opts.limit));
  if (opts?.q) params.set("q", opts.q);
  const qs = params.toString();
  return req(`/api/documents/${docId}/chunks${qs ? `?${qs}` : ""}`);
}

// 停用=从检索索引摘除（可恢复）；叶子编辑=重嵌入+重索引；父块编辑仅落库
export function updateChunk(
  chunkId: string,
  body: { enabled?: boolean; text?: string },
): Promise<ChunkItem> {
  return req(`/api/chunks/${chunkId}`, jsonInit(body, "PUT"));
}

// 原始文件直链（识别前的 .docx/.pdf/扫描图原件，Content-Disposition 恢复
// 上传原名）：用于 <a href> / iframe 浏览器原生处理，不经 fetch+blob。
// inline=true 时服务端对白名单类型（pdf/图片/纯文本）改回 inline 内联渲染，
// PDF 可再带 page 生成 #page=N 让浏览器查看器跳页（M5-2 引用定位）。
export function docOriginalUrl(
  docId: string,
  opts?: { inline?: boolean; page?: number | null },
): string {
  const base = `/api/documents/${docId}/original`;
  if (!opts?.inline) return base;
  return `${base}?disposition=inline${opts.page ? `#page=${opts.page}` : ""}`;
}

export function search(
  kbId: string,
  query: string,
  opts?: {
    topK?: number; debug?: boolean;
    // M6-1.5 请求级策略试跑覆盖（不落库）：undefined/null=按 KB 策略
    useKeyword?: boolean | null; useRerank?: boolean | null;
  },
): Promise<SearchResult> {
  return req(`/api/kb/${kbId}/search`, jsonInit({
    query, top_k: opts?.topK ?? 5, debug: opts?.debug ?? false,
    use_keyword: opts?.useKeyword ?? undefined,
    use_rerank: opts?.useRerank ?? undefined,
  }));
}

// ---- 评测回归（B）----

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

// M6-2：kbIds 多于一个=多库联合会话（跨这些库检索）；单个/不传=单库老行为。
export function createConv(kbId: string, kbIds?: string[]): Promise<Conversation> {
  const body = kbIds && kbIds.length > 1
    ? { kb_id: kbId, kb_ids: kbIds } : { kb_id: kbId };
  return req("/api/conversations", jsonInit(body));
}

export function listMessages(convId: string): Promise<Message[]> {
  return req(`/api/conversations/${convId}/messages`);
}

// 会话重命名/删除（M5-1 F2 使用端会话侧栏）：后端按归属过滤，非本人会话
// 统一 404（不区分"不存在"与"不是你的"，见 kbase/conversations.py 注释）。
export function renameConv(convId: string, title: string): Promise<Conversation> {
  return req(`/api/conversations/${convId}`, jsonInit({ title }, "PUT"));
}

export function deleteConv(convId: string): Promise<{ ok: boolean }> {
  return req(`/api/conversations/${convId}`, { method: "DELETE" });
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

// ---- 模型目录（M5-2 Provider UI：下拉选型号）----

export interface ModelCatalog {
  base_url: string;
  models: string[];
  fetched_at: string | null;
  stale: boolean;            // 超过 7 天：服务端会在 GET 时自动后台刷新
}

export interface ModelRefreshBody {
  base_url?: string;
  api_key?: string;
  api_key_env?: string;
  provider_name?: string;    // 已存 provider：用它存的凭据拉取
}

export function listModelCatalogs(): Promise<{ catalogs: ModelCatalog[] }> {
  return req("/api/settings/models");
}

export function refreshModelCatalog(body: ModelRefreshBody): Promise<ModelCatalog> {
  return req("/api/settings/models/refresh", jsonInit(body));
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

// ---- 运营看板（C）----

export interface QaTrendPoint {
  date: string;
  total: number;
  refused: number;
}

export interface QaOverview {
  days: number;
  total: number;
  refused: number;
  refusal_rate: number;
  trend: QaTrendPoint[];
}

export interface UnansweredItem {
  ts: string;
  question: string | null;
  actor: string;
  resource: string | null;
}

export function getQaStats(days = 7): Promise<QaOverview> {
  return req(`/api/stats/qa?days=${days}`);
}

export function getUnanswered(limit = 50): Promise<{ items: UnansweredItem[] }> {
  return req(`/api/stats/unanswered?limit=${limit}`);
}
