// lib/api/share.ts —— 免登录分享域（对标 #1）：
// 管理侧（editor+）建/列/撤销分享链接；公开侧 meta（免登录，分享页首屏）。
// 公开问答走 ShareView 里的原生 fetch SSE（与 useChat 同一套 parseSSE），
// 不经 req()——分享页无会话，401 拦截/跳登录逻辑都不适用。
import { jsonInit, req } from "./core";

export interface ShareLinkItem {
  id: string;
  token: string;
  name: string;
  provider: string | null;
  // 多库联查：绑定的全部库（主库首位）；单库时长度 1。kb_names 供列表显示
  // 联查范围（已删副库名自然缺席）
  kb_ids: string[];
  kb_names: string[];
  created_at: string;
}

export function createShareLink(
  kbId: string,
  body: { name?: string; provider?: string | null; extra_kb_ids?: string[] },
): Promise<{ id: string; token: string; name: string; provider: string | null;
             kb_ids: string[] }> {
  return req(`/api/kb/${kbId}/share-links`, jsonInit(body));
}

export function listShareLinks(kbId: string): Promise<ShareLinkItem[]> {
  return req(`/api/kb/${kbId}/share-links`);
}

export function revokeShareLink(linkId: string): Promise<{ ok: boolean }> {
  return req(`/api/share-links/${linkId}`, { method: "DELETE" });
}

/** 分享页首屏（免登录）：库名 + 链接备注。kb_names 为联查全量（单库长度 1）。
 * 404=链接不存在或已撤销。 */
export function getShareMeta(token: string): Promise<{
  kb_name: string; name: string; kb_names?: string[];
}> {
  return req(`/api/share/${token}`, undefined, { skipAuthRedirect: true });
}
