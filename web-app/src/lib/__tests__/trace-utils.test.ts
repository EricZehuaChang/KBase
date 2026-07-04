import { describe, expect, it } from "vitest";
import { rankChanges, shortChunkId } from "../trace-utils";

describe("rankChanges", () => {
  it("按 fused 名次与 reranked 名次的差值标注：新进/上升/下降", () => {
    const fused: [string, number][] = [["a", 0.9], ["b", 0.8], ["c", 0.7]];
    const reranked: [string, number][] = [["c", 0.95], ["a", 0.5], ["d", 0.4]];
    expect(rankChanges(fused, reranked)).toEqual({
      c: "↑2",
      a: "↓1",
      d: "新进",
    });
  });

  it("名次不变返回占位符 —", () => {
    const fused: [string, number][] = [["a", 0.9], ["b", 0.8]];
    const reranked: [string, number][] = [["a", 0.95], ["b", 0.85]];
    expect(rankChanges(fused, reranked)).toEqual({ a: "—", b: "—" });
  });

  it("fused 为空时 reranked 全部标注为新进", () => {
    const reranked: [string, number][] = [["x", 0.5]];
    expect(rankChanges([], reranked)).toEqual({ x: "新进" });
  });
});

describe("shortChunkId", () => {
  it("截取前 8 个字符作为短前缀", () => {
    expect(shortChunkId("abcdef1234567890")).toBe("abcdef12");
  });

  it("短于 8 字符原样返回", () => {
    expect(shortChunkId("ab12")).toBe("ab12");
  });
});
