import { describe, expect, it } from "vitest";
import { renderWithChips, groupByTime, appendConversationPage, originalPreviewKind } from "../chat-utils";

describe("renderWithChips", () => {
  it("将 [n] 角标切分为 chip 片段，其余为文本片段，文本内容完整不丢字符", () => {
    const segments = renderWithChips("见[1]与[2]");
    expect(segments).toEqual([
      { type: "text", text: "见" },
      { type: "chip", index: 1 },
      { type: "text", text: "与" },
      { type: "chip", index: 2 },
    ]);
    // 文本完整性：所有 text 片段 + chip 还原为 [n] 应等于原文
    const rebuilt = segments
      .map((s) => (s.type === "text" ? s.text : `[${s.index}]`))
      .join("");
    expect(rebuilt).toBe("见[1]与[2]");
  });

  it("无角标时返回单个文本片段", () => {
    expect(renderWithChips("没有引用")).toEqual([{ type: "text", text: "没有引用" }]);
  });

  it("空字符串返回空数组", () => {
    expect(renderWithChips("")).toEqual([]);
  });

  it("连续相邻角标（紧邻场景）正确切分，不丢字符也不合并", () => {
    const text = "多个来源[1][2][3]共同支持这一结论。";
    const segments = renderWithChips(text);
    expect(segments.filter((s) => s.type === "chip").map((s) => (s as { index: number }).index))
      .toEqual([1, 2, 3]);
    const rebuilt = segments
      .map((s) => (s.type === "text" ? s.text : `[${(s as { index: number }).index}]`))
      .join("");
    expect(rebuilt).toBe(text);
  });

  it("围栏代码块内的 [n] 不转 chip，原样保留为文本（免误伤伪代码 arr[1]）", () => {
    const text = "示例：\n```\narr[1] = 2\n```\n结论见[1]。";
    const segments = renderWithChips(text);
    expect(segments.filter((s) => s.type === "chip"))
      .toEqual([{ type: "chip", index: 1 }]);   // 只有代码块外的 [1] 被识别
    const rebuilt = segments
      .map((s) => (s.type === "text" ? s.text : `[${(s as { index: number }).index}]`))
      .join("");
    expect(rebuilt).toBe(text);                 // 代码块内容原样保留，不丢字符
  });

  it("行内代码 `arr[0]` 内的 [n] 不转 chip", () => {
    const text = "使用 `arr[0]` 取第一个元素，参见[2]。";
    const segments = renderWithChips(text);
    expect(segments.filter((s) => s.type === "chip")).toEqual([{ type: "chip", index: 2 }]);
  });

  it("数字区间写法 [1-3] 本就不匹配纯数字角标正则，不受影响", () => {
    const segments = renderWithChips("适用范围为第[1-3]条。");
    expect(segments.filter((s) => s.type === "chip")).toEqual([]);
  });

  it("越界引用编号（citations 数组中不存在的 n）仍解析出 chip——是否存在对应" +
     "引用是渲染层按 citations 查找后的事，解析器本身不做越界校验", () => {
    const segments = renderWithChips("参考文献[99]未提供。");
    expect(segments).toContainEqual({ type: "chip", index: 99 });
  });
});

describe("groupByTime", () => {
  const now = new Date("2026-07-05T12:00:00Z");

  it("按 updated_at 分到 今天/7天内/更早 三组", () => {
    const items = [
      { id: "a", updated_at: "2026-07-05T08:00:00Z" }, // 今天
      { id: "b", updated_at: "2026-07-01T08:00:00Z" }, // 7 天内
      { id: "c", updated_at: "2026-06-01T08:00:00Z" }, // 更早
    ];
    const groups = groupByTime(items, now);
    expect(groups.map((g) => g.label)).toEqual(["今天", "7天内", "更早"]);
    expect(groups[0].items).toEqual([items[0]]);
    expect(groups[1].items).toEqual([items[1]]);
    expect(groups[2].items).toEqual([items[2]]);
  });

  it("缺失分组时不产出空组", () => {
    const groups = groupByTime([{ id: "a", updated_at: "2026-07-05T08:00:00Z" }], now);
    expect(groups).toEqual([
      { label: "今天", items: [{ id: "a", updated_at: "2026-07-05T08:00:00Z" }] },
    ]);
  });
});

describe("appendConversationPage", () => {
  it("首次加载：existing 为空时结果即本页 items，hasMore 按 total 判断", () => {
    const result = appendConversationPage([], { items: [{ id: "a" }, { id: "b" }], total: 5 });
    expect(result.items).toEqual([{ id: "a" }, { id: "b" }]);
    expect(result.hasMore).toBe(true);
  });

  it("加载更多：新页追加在已有列表之后", () => {
    const existing = [{ id: "a" }, { id: "b" }];
    const result = appendConversationPage(existing, { items: [{ id: "c" }], total: 3 });
    expect(result.items).toEqual([{ id: "a" }, { id: "b" }, { id: "c" }]);
    expect(result.hasMore).toBe(false);        // 3 项已全部加载
  });

  it("hasMore 精确反映 items.length < total", () => {
    const result = appendConversationPage([{ id: "a" }], { items: [{ id: "b" }], total: 3 });
    expect(result.hasMore).toBe(true);          // 已有 2/3，还差 1
  });
});

describe("originalPreviewKind（M5-2 原始文件预览类型判定）", () => {
  it("pdf → 浏览器查看器内联预览", () => {
    expect(originalPreviewKind("政策文件.pdf")).toBe("pdf");
    expect(originalPreviewKind("UPPER.PDF")).toBe("pdf");
  });
  it("图片 → img 直显", () => {
    expect(originalPreviewKind("扫描件.png")).toBe("image");
    expect(originalPreviewKind("票据.JPG")).toBe("image");
    expect(originalPreviewKind("x.webp")).toBe("image");
  });
  it("office/未知/空 → null（回退 Markdown 定位+下载）", () => {
    expect(originalPreviewKind("报告.docx")).toBe(null);
    expect(originalPreviewKind("表.xlsx")).toBe(null);
    expect(originalPreviewKind("no-ext")).toBe(null);
    expect(originalPreviewKind(null)).toBe(null);
    expect(originalPreviewKind(undefined)).toBe(null);
  });
});
