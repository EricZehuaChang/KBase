// src/lib/chat-utils.ts —— 问答页纯函数（可测，不依赖 DOM/组件实例）。

/** renderWithChips 的输出片段：纯文本或引用角标。渲染层据此构建 VNode /
 * 分段渲染，任何环节都不把原始字符串塞进 v-html，避免文档内容注入。 */
export type ChipSegment =
  | { type: "text"; text: string }
  | { type: "chip"; index: number };

const CHIP_RE = /\[(\d+)\]/g;

/** 将助手回答中的 `[n]` 引用角标切分为文本/角标片段序列，供组件渲染为
 * 可点击的 accent 小圆片。纯函数：不做任何 DOM 操作，用于 vitest 直接断言。 */
export function renderWithChips(text: string): ChipSegment[] {
  const segments: ChipSegment[] = [];
  let lastIndex = 0;
  for (const match of text.matchAll(CHIP_RE)) {
    const start = match.index ?? 0;
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
