// lib/api/settings.ts —— 管理与运维域：LLM Provider CRUD/连通性测试/
// 模型目录、向量模型密钥、用户与 API Key 管理、许可证、健康检查、
// 运营看板统计（问答量/拒答/反馈）。
import { jsonInit, req } from "./core";

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

export interface ProvidersResponse {
  active: string | null;
  providers: Provider[];
}

export interface ProviderTestResult {
  ok: boolean;
  latency_ms?: number;
  error?: string;
}

export interface HealthzResponse {
  status: string;
  embedder: string;
  vectorstore: string;
  reranker: "on" | "off" | "degraded";
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

// ---- 向量模型密钥页面配置：DB 覆盖 > api_key_env（与 Provider 同规矩）----

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

// ---- 飞书连接器凭据（页面维护，secret 脱敏） ----

export interface FeishuStatus {
  configured: boolean;
  app_id: string | null;
  secret_hint: string | null;
}

export function getFeishuStatus(): Promise<FeishuStatus> {
  return req("/api/settings/feishu");
}

export function putFeishuCredentials(appId: string, appSecret: string): Promise<{ ok: boolean }> {
  return req("/api/settings/feishu",
             jsonInit({ app_id: appId, app_secret: appSecret }, "PUT"));
}

export function deleteFeishuCredentials(): Promise<{ ok: boolean }> {
  return req("/api/settings/feishu", { method: "DELETE" });
}

export function healthz(): Promise<HealthzResponse> {
  return req("/healthz");
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

// ---- 运营看板（C + M6-4）----

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
