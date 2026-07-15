// lib/api/chat.ts —— 问答与会话域：检索调试、会话 CRUD、SSE 问答端点
// （返回原始 Response 交给 parseSSE）、消息反馈提交。
import { jsonInit, req } from "./core";

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
  // 多模态回答（图片一期）：命中页的文档内嵌插图（文本层 PDF 才有）。
  // 前端在答案下方渲染缩略图；图片不进 LLM prompt，纯检索事实关联。
  images?: { url: string; name: string; width: number; height: number }[];
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

export interface QueryBody {
  question: string;
  provider?: string | null;
  top_k?: number;
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

// M6-4 反馈闭环：对助手消息点赞(1)/点踩(-1)，重复提交覆盖
export function submitFeedback(
  messageId: string, rating: 1 | -1, note?: string,
): Promise<{ message_id: string; rating: number }> {
  return req(`/api/messages/${messageId}/feedback`, jsonInit({ rating, note }));
}
