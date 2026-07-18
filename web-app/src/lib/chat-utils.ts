// src/lib/chat-utils.ts —— 问答页纯函数（可测，不依赖 DOM/组件实例）。

/** renderWithChips 的输出片段：纯文本或引用角标。渲染层据此构建 VNode /
 * 分段渲染，任何环节都不把原始字符串塞进 v-html，避免文档内容注入。 */
export type ChipSegment =
  | { type: "text"; text: string }
  | { type: "chip"; index: number };

const CHIP_RE = /\[(\d+)\]/g;

// M5-1 F2：代码块/行内代码免疫区间——LLM 回答里偶尔会带示例代码（如
// `arr[1]`），这类 [n] 不该被误判成引用角标。只做轻量启发式，不是完整
// CommonMark 解析：围栏代码块（```...```，可跨行）与行内代码（`...`，
// 规约不跨行）各自用正则整体圈出区间，角标扫描命中这些区间时当纯文本
// 跳过。数字区间（如"[1-3]"）天然不受影响——CHIP_RE 本身只匹配纯数字，
// 带连字符的内容匹配不上，不需要额外过滤。
function protectedRanges(text: string): Array<[number, number]> {
  const ranges: Array<[number, number]> = [];
  for (const m of text.matchAll(/```[\s\S]*?```/g)) {
    ranges.push([m.index ?? 0, (m.index ?? 0) + m[0].length]);
  }
  // 行内代码复用同一个数组做"是否已被围栏代码块覆盖"的判断，避免同一个
  // `[n]` 位置被重复统计——不影响正确性，只是省一次去重。
  for (const m of text.matchAll(/`[^`\n]+`/g)) {
    const start = m.index ?? 0;
    if (!ranges.some(([s, e]) => start >= s && start < e)) {
      ranges.push([start, start + m[0].length]);
    }
  }
  return ranges;
}

/** 将助手回答中的 `[n]` 引用角标切分为文本/角标片段序列，供组件渲染为
 * 可点击的 accent 小圆片。纯函数：不做任何 DOM 操作，用于 vitest 直接断言。
 * 不做"角标编号是否存在对应引用"的越界校验——解析器不知道 citations 数组
 * 内容，找不到对应引用是渲染层的事（按 index 查 citations，找不到时展示
 * "引用不存在"兜底），职责分离，纯函数不依赖会话状态更方便单测。 */
/** 回答正文实际引用到的角标编号集合（1-based，对应 citations 下标+1）。
 * 附图区据此只展示"回答真的用到了"的引用的插图——top-k 里没被引用的
 * 边缘命中（往往正是不相关章节）不再往回答里灌图。 */
export function citedIndexSet(text: string): Set<number> {
  const out = new Set<number>();
  for (const seg of renderWithChips(text)) {
    if (seg.type === "chip") out.add(seg.index);
  }
  return out;
}

export function renderWithChips(text: string): ChipSegment[] {
  const segments: ChipSegment[] = [];
  const guarded = protectedRanges(text);
  let lastIndex = 0;
  for (const match of text.matchAll(CHIP_RE)) {
    const start = match.index ?? 0;
    if (guarded.some(([s, e]) => start >= s && start < e)) continue;
    if (start > lastIndex) {
      segments.push({ type: "text", text: text.slice(lastIndex, start) });
    }
    segments.push({ type: "chip", index: Number(match[1]) });
    lastIndex = start + match[0].length;
  }
  if (lastIndex < text.length) {
    segments.push({ type: "text", text: text.slice(lastIndex) });
  }
  return segments;
}

/** 分页累加结果：items 为累加后的完整列表，hasMore 表示是否还有更多可加载
 * （items.length < total）。侧栏"加载更多"按钮据此决定是否显示。 */
export interface AppendedPage<T> {
  items: T[];
  hasMore: boolean;
}

/** 将新一页会话追加到已有列表之后（分页"加载更多"用的纯累加函数，不做
 * 去重——offset 分页调用方保证不重叠）。首次加载时 existing 传空数组。 */
export function appendConversationPage<T>(
  existing: T[],
  page: { items: T[]; total: number },
): AppendedPage<T> {
  const items = [...existing, ...page.items];
  return { items, hasMore: items.length < page.total };
}

/** 会话分组标签。 */
export type TimeGroup = "今天" | "7天内" | "更早";

export interface Groupable {
  updated_at?: string;
}

/** 按 updated_at 把会话分到 今天/7天内/更早 三组，保持组内原有顺序（调用方
 * 已按 updated_at desc 排序）。缺失 updated_at 的项归入"更早"。 */
export function groupByTime<T extends Groupable>(
  items: T[],
  now: Date = new Date(),
): { label: TimeGroup; items: T[] }[] {
  const startOfToday = new Date(now.getFullYear(), now.getMonth(), now.getDate()).getTime();
  const sevenDaysAgo = startOfToday - 6 * 24 * 60 * 60 * 1000;

  const today: T[] = [];
  const recent: T[] = [];
  const older: T[] = [];

  for (const item of items) {
    const ts = item.updated_at ? new Date(item.updated_at).getTime() : NaN;
    if (!Number.isNaN(ts) && ts >= startOfToday) today.push(item);
    else if (!Number.isNaN(ts) && ts >= sevenDaysAgo) recent.push(item);
    else older.push(item);
  }

  const groups: { label: TimeGroup; items: T[] }[] = [];
  if (today.length) groups.push({ label: "今天", items: today });
  if (recent.length) groups.push({ label: "7天内", items: recent });
  if (older.length) groups.push({ label: "更早", items: older });
  return groups;
}

/** highlightSegments 的输出片段：纯文本或命中高亮。渲染层用 <mark> 包裹
 * highlight 片段，其余原样输出为文本节点——绝不经过 v-html，避免文档/
 * 用户输入拼接的字符串进入 innerHTML。 */
export type HighlightSegment =
  | { type: "text"; text: string }
  | { type: "highlight"; text: string };

function escapeRegExp(s: string): string {
  return s.replace(/[.*+?^${}()|[\]\\]/g, "\\$&");
}

/** 将 needle（通常是用户提问）中长度 >=2 的词元在 haystack 中做大小写不敏感
 * 高亮切分；needle 为空或未命中时返回单个文本片段。用于引用抽屉 snippet
 * 高亮与全文预览锚点高亮，纯函数、无 DOM 依赖，可直接单测。 */
export function highlightSegments(haystack: string, needle: string): HighlightSegment[] {
  const terms = Array.from(
    new Set(
      needle
        .split(/[\s,，。？?！!、；;：:]+/)
        .map((t) => t.trim())
        .filter((t) => t.length >= 2),
    ),
  ).sort((a, b) => b.length - a.length);

  if (terms.length === 0 || !haystack) return [{ type: "text", text: haystack }];

  const pattern = new RegExp(`(${terms.map(escapeRegExp).join("|")})`, "gi");
  const segments: HighlightSegment[] = [];
  let lastIndex = 0;
  for (const match of haystack.matchAll(pattern)) {
    const start = match.index ?? 0;
    if (start > lastIndex) segments.push({ type: "text", text: haystack.slice(lastIndex, start) });
    segments.push({ type: "highlight", text: match[0] });
    lastIndex = start + match[0].length;
  }
  if (lastIndex < haystack.length) segments.push({ type: "text", text: haystack.slice(lastIndex) });
  return segments.length ? segments : [{ type: "text", text: haystack }];
}

// ---- 原始文件预览类型判定（M5-2 引用定位）----

export type PreviewKind = "pdf" | "image" | null;

const IMAGE_EXTS = new Set(["png", "jpg", "jpeg", "bmp", "webp"]);

/** 按文件名判断原始文件能否在浏览器内联预览：pdf → 浏览器自带查看器
 * （支持 #page=N 跳页定位）；图片 → <img> 直显；其余（docx/xlsx 等）
 * 浏览器无法原生渲染，返回 null——调用方回退到"Markdown 全文定位 +
 * 下载原件"。与后端 _INLINE_MEDIA_TYPES 白名单语义对齐。 */
export function originalPreviewKind(filename: string | null | undefined): PreviewKind {
  const ext = (filename ?? "").split(".").pop()?.toLowerCase() ?? "";
  if (ext === "pdf") return "pdf";
  if (IMAGE_EXTS.has(ext)) return "image";
  return null;
}

// ---- 回答气泡的行内 Markdown 渲染（生产反馈：LLM 输出的 **加粗** 原样
// 显示星号）。安全边界：输入先全量 HTML 转义，之后只由本函数拼接白名单
// 标签（strong/em/code）——进 v-html 的是受控产物而非原始字符串，与
// "不把原始字符串塞进 v-html"的防注入原则实质一致。
// 只做行内格式；块级（表格/多级列表）留给后续专项。 ----

export function escapeHtml(s: string): string {
  return s.replace(/&/g, "&amp;").replace(/</g, "&lt;")
    .replace(/>/g, "&gt;").replace(/"/g, "&quot;");
}

/** 行内 Markdown → 受控 HTML：`code`（内部不再处理其他标记）、**bold**、
 * *italic*（保守匹配，避免误伤单个星号）。 */
export function inlineMarkdownHtml(text: string): string {
  let html = escapeHtml(text);
  const codes: string[] = [];
  // 行内代码先行占位——code 内的 ** / * 属字面内容，不参与加粗斜体。
  // 哨兵含转义后不可能出现在正文里的形态（"@@KBCODE数字@@"配合唯一批次）。
  html = html.replace(/`([^`\n]+)`/g, (_, c: string) => {
    codes.push(c);
    return `@@KBCODE${codes.length - 1}@@`;
  });
  html = html.replace(/\*\*([^*\n]+)\*\*/g, "<strong>$1</strong>");
  html = html.replace(/(^|[^*])\*([^*\n]+)\*(?!\*)/g, "$1<em>$2</em>");
  html = html.replace(/@@KBCODE(\d+)@@/g, (_, i: string) =>
    `<code class="rounded bg-[var(--surface-2)] px-1 py-0.5 text-[0.9em]">${codes[Number(i)]}</code>`);
  return html;
}
