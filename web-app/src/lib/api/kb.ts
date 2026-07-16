// lib/api/kb.ts —— 知识库与文档域：库 CRUD/授权/配置/换绑、文档上传
// （含 XHR 进度）/审核/重试/原文直链、Chunk 运营管理、URL 导入、演示数据。
import { handleUnauthorized, jsonInit, req } from "./core";

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
  // admin 可经 rebindEmbedder 换绑（全库向量作废后台重建）。
  embedder?: string;
  retrieval?: KbRetrievalConfig;
}

export interface Kb {
  id: string;
  name: string;
  // 文档数随库列表一次返回（服务端 group by），卡片计数不再异步跳变；
  // createKb 的响应没有该字段（新库恒为 0），故可选
  doc_count?: number;
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

// M6-7 URL 连接器：拉取网页导入知识库（内网 wiki/门户为主用途）
export function importUrl(kbId: string, url: string): Promise<{ accepted: string[] }> {
  return req(`/api/kb/${kbId}/import-url`, jsonInit({ url }));
}

// E POC 演示数据一键装载：幂等——演示库已存在时 created=false 直接返回其 id
export function loadDemoData(): Promise<{ id: string; name: string; created: boolean }> {
  return req("/api/demo-data", { method: "POST" });
}
