import { describe, expect, it } from "vitest";
import { renderWithChips, groupByTime } from "../chat-utils";

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
