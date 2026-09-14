// lib/api/share.ts —— 免登录分享域（对标 #1）：
// 管理侧（editor+）建/列/撤销分享链接；公开侧 meta（免登录，分享页首屏）。
// 公开问答走 ShareView 里的原生 fetch SSE（与 useChat 同一套 parseSSE），
// 不经 req()——分享页无会话，401 拦截/跳登录逻辑都不适用。
import { jsonInit, req } from "./core";

// T10：访问口令请求头（与后端 kbase/api/routes/share.py 同名常量对齐）。
// 口令走请求头不走 query——query 会进网关/访问日志。
export const SHARE_PASSWORD_HEADER = "X-Share-Password";

export interface ShareLinkItem {
  id: string;
  token: string;
  name: string;
  provider: string | null;
  // 多库联查：绑定的全部库（主库首位）；单库时长度 1。kb_names 供列表显示
  // 联查范围（已删副库名自然缺席）
  kb_ids: string[];
  kb_names: string[];
  // T10 策略与用量。null=该维度不限（后端 NULL 语义：expires_at=NULL 永不过期、
  // max_visits=NULL 不限次数、visit_count=NULL 从未计次）；has_password 只说
  // "这条链接要不要口令"——哈希后端从不回传。
  expires_at: string | null;
  has_password: boolean;
  max_visits: number | null;
  visit_count: number | null;
  created_at: string;
}

export function createShareLink(
  kbId: string,
  body: {
    name?: string; provider?: string | null; extra_kb_ids?: string[];
    // T10 可选策略（不填/null=不限）。expires_at 只发日期 YYYY-MM-DD，后端按
    // 当日 23:59:59 收口（与 ApiKey 有效期同一约定，选"今天"不会立刻过期）。
    expires_at?: string | null; password?: string | null;
    max_visits?: number | null;
  },
): Promise<{ id: string; token: string; name: string; provider: string | null;
             kb_ids: string[];
             expires_at: string | null; has_password: boolean;
             max_visits: number | null; visit_count: number | null }> {
  return req(`/api/kb/${kbId}/share-links`, jsonInit(body));
}

export function listShareLinks(kbId: string): Promise<ShareLinkItem[]> {
  return req(`/api/kb/${kbId}/share-links`);
}

export function revokeShareLink(linkId: string): Promise<{ ok: boolean }> {
  return req(`/api/share-links/${linkId}`, { method: "DELETE" });
}

/** 分享页首屏结果（T10）：三种状态必须分开——口令要弹框输入、失效要整页
 * 替换成失效提示。混成一个 reject 会让分享页分不清该做哪件事。 */
export type ShareMetaResult =
  | { state: "ok"; kb_name: string; name: string; kb_names: string[] }
  | { state: "password" }      // 401：该链接需要口令（或口令不正确）
  | { state: "invalid" };      // 404：不存在/已撤销/已过期/次数用尽

/** 分享页首屏（免登录）：库名 + 链接备注。kb_names 为联查全量（单库长度 1）。
 *
 * T10 起手工 fetch 而不是走 req()：req() 把 401/404 都收敛成一个 Error（连
 * 状态码都拿不到），而分享页恰恰要靠状态码分流（401=弹口令框，404=整页失效）；
 * 分享页无会话，401 也不该触发全局登录跳转。 */
export async function getShareMeta(
  token: string, password?: string | null,
): Promise<ShareMetaResult> {
  const res = await fetch(`/api/share/${encodeURIComponent(token)}`, {
    headers: password ? { [SHARE_PASSWORD_HEADER]: password } : undefined,
  });
  if (res.status === 401) return { state: "password" };
  if (!res.ok) return { state: "invalid" };
  const meta = (await res.json()) as {
    kb_name: string; name: string; kb_names?: string[];
  };
  return { state: "ok", kb_name: meta.kb_name, name: meta.name,
           kb_names: meta.kb_names ?? [] };
}
