// lib/api/settings.ts —— 管理与运维域：LLM Provider CRUD/连通性测试/
// 模型目录、向量模型密钥、用户与 API Key 管理、许可证、健康检查、
// 运营看板统计（问答量/拒答/反馈）。
import { jsonInit, req } from "./core";
// T12 归因下钻的 citations 复用问答域的 Citation 定义（同一份引用结构，不另造）
import type { Citation } from "./chat";

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

// ---- 发件箱（SMTP，账号通知等系统邮件；密码只写不回显） ----

export interface SmtpStatus {
  configured: boolean;
  host: string | null;
  port: number;
  user: string | null;
  from_addr: string | null;
  from_name: string | null;
  has_password: boolean;
  /** 系统邮件语言：auto=跟随收件人账号语言，zh/en=强制 */
  language: "auto" | "zh" | "en";
}

export function getSmtpSettings(): Promise<SmtpStatus> {
  return req("/api/settings/smtp");
}

export function putSmtpSettings(body: {
  host: string; port: number; user: string;
  password?: string | null; from_addr: string; from_name: string;
  language?: "auto" | "zh" | "en";
}): Promise<{ ok: boolean }> {
  return req("/api/settings/smtp", jsonInit(body, "PUT"));
}

export function testSmtp(to: string): Promise<{ ok: boolean }> {
  return req("/api/settings/smtp/test", jsonInit({ to }));
}

// ---- 飞书群机器人（对标 #2：群里 @机器人 问答） ----

export interface FeishuBotStatus {
  configured: boolean;
  has_verification_token: boolean;
  has_encrypt_key: boolean;
  kb_id: string | null;
  provider: string | null;
}

export function getFeishuBot(): Promise<FeishuBotStatus> {
  return req("/api/settings/feishu-bot");
}

export function putFeishuBot(body: {
  verification_token?: string | null; encrypt_key?: string | null;
  kb_id: string; provider?: string | null;
}): Promise<FeishuBotStatus> {
  return req("/api/settings/feishu-bot", jsonInit(body, "PUT"));
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
  email: string | null;
  role: string;
  disabled: boolean;
  advanced_ui: boolean;   // viewer 高级界面开关（editor/admin 恒视为开）
  created_at: string;
}

export interface UserCreateBody {
  username: string;
  role: string;
  password: string;
  email?: string;
}

export interface UserUpdateBody {
  username?: string;      // 改账号名（仅超管）：旧名会话失效须重新登录
  role?: string;
  disabled?: boolean;
  password?: string;
  email?: string;         // 空串=清除邮箱
  advanced_ui?: boolean;  // viewer 高级界面开关
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

/** 删除账号（仅超管）：连带清理其私有数据（会话/消息/反馈/授权行）；
 * 团队资产（知识库/文档）不随人删。不可恢复——调用方必须先弹确认。 */
export function deleteUser(id: string): Promise<{ ok: boolean }> {
  return req(`/api/users/${id}`, { method: "DELETE" });
}

// ---- 自定义角色（清单 admin 可读；增改删仅超管）----

export interface RoleItem {
  name: string;
  label: string;
  builtin: boolean;         // 内置四角色：权限只读、不可改删
  permissions: string[];    // 权限词表子集
}

export function listRoles(): Promise<{ roles: RoleItem[]; permissions: string[] }> {
  return req("/api/roles");
}

export function createRole(body: {
  name: string; label?: string; permissions?: string[];
}): Promise<RoleItem> {
  return req("/api/roles", jsonInit(body));
}

export function updateRole(name: string, body: {
  label?: string; permissions?: string[];
}): Promise<RoleItem> {
  return req(`/api/roles/${name}`, jsonInit(body, "PUT"));
}

/** 删除自定义角色：仍有用户在用时后端 422（先改派再删）。 */
export function deleteRole(name: string): Promise<{ ok: boolean }> {
  return req(`/api/roles/${name}`, { method: "DELETE" });
}

/** 邀请用户（「邮箱与邀请」）：可顺带维护邮箱；password 留空=随机生成。
 * 后端设置新初始密码并**同步**发送"登录地址/账号/初始密码"邮件（按账号
 * 语言偏好选中/英文模板），发信失败回错误且不动密码。 */
export function inviteUser(
  id: string, body: { email?: string; password?: string },
): Promise<{ ok: boolean; email: string }> {
  return req(`/api/users/${id}/invite`, jsonInit(body));
}

// ---- API Key 管理（M4-1 G6，admin；T09 追加有效期/白名单/配额/用量）----

export interface ApiKeyItem {
  id: string;
  name: string;
  prefix: string;
  role: string;
  revoked: boolean;              // 吊销（不可恢复，DELETE 端点）
  disabled: boolean;             // 停用（可恢复，PATCH 开关）
  scope_kb_ids: string[] | null; // 库白名单；null=不限
  ip_allow: string[] | null;     // 来源 IP 白名单（精确 IP 或 CIDR）；null=不限
  rpm: number | null;            // 每分钟请求上限；null=不限
  daily_quota: number | null;    // 每日请求上限；null=不限
  expires_at: string | null;     // 过期时间（naive UTC ISO）；null=永不过期
  last_used_at: string | null;   // 最近使用（naive UTC ISO）；null=从未使用
  created_at: string;
}

/** 策略字段（T09）：创建与 PATCH 共用。null 一律=该维度不限——PATCH 时
 * 显式传 null 即"清除该项限制"（缺省字段才是"不动"，见 buildApiKeyPolicy）。 */
export interface ApiKeyPolicyBody {
  expires_at?: string | null;
  rpm?: number | null;
  daily_quota?: number | null;
  ip_allow?: string[] | null;
}

export interface ApiKeyCreateBody extends ApiKeyPolicyBody {
  name: string;
  role: string;
  scope_kb_ids?: string[] | null;
}

export interface ApiKeyCreated extends ApiKeyItem {
  key: string; // 完整 key，仅创建时返回一次
}

export interface ApiKeyUsageDay {
  day: string;                   // UTC 日期 YYYY-MM-DD
  requests: number;
  prompt_tokens: number;
  completion_tokens: number;
  tokens_estimated: boolean;     // true=token 数为按字符数的估算值
}

export interface ApiKeyUsage {
  key_id: string;
  name: string;
  days: number;
  rpm: number | null;
  daily_quota: number | null;
  items: ApiKeyUsageDay[];
  totals: { requests: number; prompt_tokens: number; completion_tokens: number };
  tokens_estimated: boolean;
}

export function listApiKeys(): Promise<ApiKeyItem[]> {
  return req("/api/settings/api-keys");
}

export function createApiKey(body: ApiKeyCreateBody): Promise<ApiKeyCreated> {
  return req("/api/settings/api-keys", jsonInit(body));
}

/** 改 Key 策略（T09）：启停/配额/延期/IP 白名单。role 与 scope_kb_ids 不在
 * 其中——换角色、换库白名单等于换一把钥匙，重建更清楚。 */
export function updateApiKey(
  id: string, body: ApiKeyPolicyBody & { disabled?: boolean },
): Promise<ApiKeyItem> {
  return req(`/api/settings/api-keys/${id}`, jsonInit(body, "PATCH"));
}

/** 某 Key 的近 N 天用量（T09）；per-key 用量只在管理端鉴权接口暴露。 */
export function getApiKeyUsage(id: string, days = 30): Promise<ApiKeyUsage> {
  return req(`/api/settings/api-keys/${id}/usage?days=${days}`);
}

export function revokeApiKey(id: string): Promise<{ ok: boolean }> {
  return req(`/api/settings/api-keys/${id}`, { method: "DELETE" });
}

// ---- 许可证（M4-1 G6）----

export type LicenseStatus = "trial" | "valid" | "expired" | "invalid";

export interface LicenseInfo {
  status: LicenseStatus;
  org?: string | null;
  expires?: string | null;
  /** T16：v2 证书的版本/座位/功能位；v1 老证书这几个字段为 null */
  edition?: string | null;
  seats?: number | null;
  features?: string[] | null;
  format?: "v1" | "v2" | null;
  /** 到期后剩余宽限天数（到期日为第 0 天）；未到期/无证书为 null */
  grace_days_left?: number | null;
}

/** T16 离线续期结果：服务端验签并原子落盘后返回新证书状态。 */
export interface LicenseUploadResult {
  ok: boolean;
  filename: string;
  license: LicenseInfo;
}

export function getLicense(): Promise<LicenseInfo> {
  return req("/api/license");
}

/** T16 离线续期：管理员上传新的 license.json（multipart，字段名 file）。
 * 服务端先验签再原子替换，成功后立即生效（不需要重启）；签名不过的文件
 * 不会落盘，原证书保持可用。 */
export function uploadLicense(file: File): Promise<LicenseUploadResult> {
  const form = new FormData();
  form.append("file", file);
  // 不手写 Content-Type：multipart 的 boundary 必须由浏览器生成
  return req("/api/license", { method: "POST", body: form });
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

// ---- 问答归因（T12）：每次问答一行，四个互斥桶 ----
//
// 数据只能从上线后开始积累：历史问答当时没记命中数/最高分，**无法回填**（重跑
// 历史问题拿到的是今天的检索结果，不是当时的事故现场）。所以面板上线初期行数
// 会很少，第一份有意义的报告要等数据攒够（周量级）——不要在界面上写"立刻可见"。

/** 归因桶。取值与后端 kbase/qa_outcomes.py 的模块常量一一对应（不是自由文本）：
 * empty_retrieval=检索为空（可能没这份资料）；below_threshold=检索到了但全低于
 * 阈值（资料在库里却捞不起来，与前者运营动作完全不同）；scope_denied=API Key
 * 越权静默空集（安全事件）；answered=正常作答。点踩不单列成桶——它是同一行上的
 * feedback=-1。 */
export type OutcomeBucket =
  | "empty_retrieval" | "below_threshold" | "scope_denied" | "answered";

export interface OutcomeItem {
  id: string;
  ts: string;
  channel: string;
  kb_id: string | null;
  kb_ids: string[] | null;
  conv_id: string | null;
  message_id: string | null;
  actor: string | null;
  bucket: OutcomeBucket;
  retrieved_count: number;
  usable_count: number;
  /** 本轮最高检索分；检索为空时为 null（不是 0——阈值量纲随检索模式变，0 未必是低分） */
  top_score: number | null;
  question: string;
  /** null=未评，1=赞，-1=踩（后端 feedback.upsert_feedback 同步归因行） */
  feedback: number | null;
}

export interface OutcomePage {
  items: OutcomeItem[];
  total: number;
  /** 按桶分布。**不叠加 bucket 过滤**（其余过滤照用）——否则按桶点进去就只看得到
   * 自己那一格，等于没有分布。 */
  buckets: Record<string, number>;
  buckets_known: OutcomeBucket[];
  limit: number;
  offset: number;
}

/** 下钻：归因行 + 该轮助手消息原文与 citations。非会话渠道（/v1、飞书、直问）
 * 没有 message_id，answer/citations 为 null（不是空串——空串会被读成"答了但答案
 * 是空的"）。 */
export interface OutcomeDetail extends OutcomeItem {
  answer: string | null;
  citations: Citation[] | null;
}

export interface OutcomeQuery {
  bucket?: string;
  channel?: string;
  kbId?: string;
  from?: string;
  to?: string;
  limit?: number;
  offset?: number;
}

function outcomeQueryString(opts: OutcomeQuery): string {
  const q = new URLSearchParams();
  if (opts.bucket) q.set("bucket", opts.bucket);
  if (opts.channel) q.set("channel", opts.channel);
  if (opts.kbId) q.set("kb_id", opts.kbId);
  if (opts.from) q.set("from", opts.from);
  if (opts.to) q.set("to", opts.to);
  q.set("limit", String(opts.limit ?? 50));
  q.set("offset", String(opts.offset ?? 0));
  return q.toString();
}

export function listOutcomes(opts: OutcomeQuery = {}): Promise<OutcomePage> {
  return req(`/api/stats/outcomes?${outcomeQueryString(opts)}`);
}

export function getOutcome(id: string): Promise<OutcomeDetail> {
  return req(`/api/stats/outcomes/${encodeURIComponent(id)}`);
}

/** CSV 导出的直链（列完整：含问题全文与 feedback）。用 <a download> 触发，不走
 * fetch——导出是文件下载，交给浏览器处理进度与另存（与 T18 批次导出同一手法）。 */
export function outcomeExportUrl(opts: OutcomeQuery = {}): string {
  return `/api/stats/outcomes/export.csv?${outcomeQueryString(opts)}`;
}

// ---- 渠道身份映射（T19）：外部渠道账号 ↔ KBase 用户 ----
//
// 这一层决定"渠道里的某个人在 KBase 里是谁"：映射后库级权限与登录态问答一致
// （有权的人查得到、无权的人被静默拒答）；未映射按匿名 viewer 处理——公开库
// 能问、收紧过的库问不到（**不是全放行**）。全部端点 admin 门槛。

export interface ChannelOption {
  /** 渠道码：feishu（现役）。取值与后端 channels.core.CHANNELS 一致 */
  channel: string;
  label: string;
}

export interface ChannelIdentity {
  id: string;
  channel: string;
  /** 渠道内的外部账号 id（飞书=sender open_id，本服务只当不透明字符串比对） */
  external_user_id: string;
  user_id: string;
  /** 绑定的用户被删除后为 null（绑定行保留，界面显示缺失态提示清理） */
  username: string | null;
  role: string | null;
  disabled: boolean | null;
  created_at: string;
}

export function listChannels(): Promise<{ items: ChannelOption[] }> {
  return req("/api/channels");
}

export function listChannelIdentities(channel?: string):
    Promise<{ items: ChannelIdentity[] }> {
  const q = channel ? `?channel=${encodeURIComponent(channel)}` : "";
  return req(`/api/channels/identities${q}`);
}

/** 绑定（同一渠道内同一个外部账号 = 改绑，后端覆盖那一行不留双份）。 */
export function bindChannelIdentity(body: {
  channel: string; external_user_id: string; user_id: string;
}): Promise<ChannelIdentity> {
  return req("/api/channels/identities", jsonInit(body));
}

/** 解绑：该外部账号立刻回落默认策略（后端每次问答现查，不缓存）。 */
export function unbindChannelIdentity(id: string): Promise<{ ok: boolean }> {
  return req(`/api/channels/identities/${encodeURIComponent(id)}`,
             { method: "DELETE" });
}

// 审计日志（admin 及以上；后端按查看者分层——超管看全量，普通 admin 的
// 视图里不出现超管 actor 的行，total 同口径，见 kbase/api/routes/admin.py）
export interface AuditItem {
  id: string;
  ts: string;
  actor: string;
  action: string;
  resource: string | null;
  detail: string | null;
  ip: string | null;
}

export function listAudit(limit = 200, offset = 0):
    Promise<{ items: AuditItem[]; total: number }> {
  return req(`/api/audit?limit=${limit}&offset=${offset}`);
}
