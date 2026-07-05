import { describe, expect, it } from "vitest";
import {
  addSection, removeSection, moveSection, jobStatusBadge, jobTypeLabel, jobHasArtifact,
  type OutlineSection,
} from "../generate-utils";

function sections(...titles: string[]): OutlineSection[] {
  return titles.map((title) => ({ title, brief: `brief-${title}` }));
}

describe("addSection", () => {
  it("默认追加到末尾", () => {
    const result = addSection(sections("A", "B"));
    expect(result).toEqual([
      { title: "A", brief: "brief-A" },
      { title: "B", brief: "brief-B" },
      { title: "", brief: "" },
    ]);
  });

  it("在指定下标之后插入", () => {
    const result = addSection(sections("A", "B"), 0);
    expect(result.map((s) => s.title)).toEqual(["A", "", "B"]);
  });

  it("不修改入参数组（纯函数）", () => {
    const original = sections("A");
    const result = addSection(original);
    expect(result).not.toBe(original);
    expect(original).toHaveLength(1);
  });
});

describe("removeSection", () => {
  it("删除指定下标的节", () => {
    const result = removeSection(sections("A", "B", "C"), 1);
    expect(result.map((s) => s.title)).toEqual(["A", "C"]);
  });

  it("越界下标（负数）时原样返回", () => {
    const original = sections("A", "B");
    const result = removeSection(original, -1);
    expect(result.map((s) => s.title)).toEqual(["A", "B"]);
  });

  it("越界下标（超出长度）时原样返回", () => {
    const original = sections("A", "B");
    const result = removeSection(original, 5);
    expect(result.map((s) => s.title)).toEqual(["A", "B"]);
  });

  it("不修改入参数组", () => {
    const original = sections("A", "B");
    const result = removeSection(original, 0);
    expect(result).not.toBe(original);
    expect(original).toHaveLength(2);
  });
});

describe("moveSection", () => {
  it("上移中间节", () => {
    const result = moveSection(sections("A", "B", "C"), 1, "up");
    expect(result.map((s) => s.title)).toEqual(["B", "A", "C"]);
  });

  it("下移中间节", () => {
    const result = moveSection(sections("A", "B", "C"), 1, "down");
    expect(result.map((s) => s.title)).toEqual(["A", "C", "B"]);
  });

  it("首节上移时保持不变（边界）", () => {
    const result = moveSection(sections("A", "B"), 0, "up");
    expect(result.map((s) => s.title)).toEqual(["A", "B"]);
  });

  it("末节下移时保持不变（边界）", () => {
    const result = moveSection(sections("A", "B"), 1, "down");
    expect(result.map((s) => s.title)).toEqual(["A", "B"]);
  });

  it("单节数组任意方向移动均保持不变", () => {
    expect(moveSection(sections("A"), 0, "up").map((s) => s.title)).toEqual(["A"]);
    expect(moveSection(sections("A"), 0, "down").map((s) => s.title)).toEqual(["A"]);
  });

  it("不修改入参数组", () => {
    const original = sections("A", "B");
    const result = moveSection(original, 0, "down");
    expect(result).not.toBe(original);
    expect(original.map((s) => s.title)).toEqual(["A", "B"]);
  });
});

describe("jobStatusBadge", () => {
  it("映射已知状态到中文标签与语义色", () => {
    expect(jobStatusBadge("done")).toEqual({ label: "已完成", class: "bg-[var(--ok-weak)] text-[var(--ok)]" });
    expect(jobStatusBadge("done_with_errors")).toEqual({ label: "部分完成", class: "bg-[var(--warn-weak)] text-[var(--warn)]" });
    expect(jobStatusBadge("failed")).toEqual({ label: "失败", class: "bg-[var(--err-weak)] text-[var(--err)]" });
    expect(jobStatusBadge("running")).toEqual({ label: "进行中", class: "bg-[var(--warn-weak)] text-[var(--warn)]" });
    expect(jobStatusBadge("pending")).toEqual({ label: "等待中", class: "bg-[var(--surface-2)] text-[var(--text-2)]" });
  });

  it("未知状态兜底中性灰并展示原始值", () => {
    expect(jobStatusBadge("weird")).toEqual({ label: "weird", class: "bg-[var(--surface-2)] text-[var(--text-2)]" });
  });
});

describe("jobTypeLabel", () => {
  it("映射 proposal/digest 到中文标签，未知类型原样返回", () => {
    expect(jobTypeLabel("proposal")).toBe("方案生成");
    expect(jobTypeLabel("digest")).toBe("定期汇编");
    expect(jobTypeLabel("other")).toBe("other");
  });
});

describe("jobHasArtifact", () => {
  it("done 与 done_with_errors 视为有产物，其余状态否", () => {
    expect(jobHasArtifact("done")).toBe(true);
    expect(jobHasArtifact("done_with_errors")).toBe(true);
    expect(jobHasArtifact("running")).toBe(false);
    expect(jobHasArtifact("failed")).toBe(false);
    expect(jobHasArtifact("pending")).toBe(false);
  });
});
