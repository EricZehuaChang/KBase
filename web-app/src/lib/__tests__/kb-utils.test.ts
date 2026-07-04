import { describe, expect, it } from "vitest";
import { statusBadge } from "../kb-utils";

describe("statusBadge", () => {
  it("映射状态到中文标签与语义色（未知状态兜底中性灰）", () => {
    expect(statusBadge("ready")).toEqual({ label: "就绪", class: "bg-[var(--ok-weak)] text-[var(--ok)]" });
    expect(statusBadge("parsing")).toEqual({ label: "解析中", class: "bg-[var(--warn-weak)] text-[var(--warn)]" });
    expect(statusBadge("pending_ocr")).toEqual({ label: "待OCR", class: "bg-[var(--warn-weak)] text-[var(--warn)]" });
    expect(statusBadge("failed")).toEqual({ label: "失败", class: "bg-[var(--err-weak)] text-[var(--err)]" });
    expect(statusBadge("weird")).toEqual({ label: "weird", class: "bg-[var(--surface-2)] text-[var(--text-2)]" });
  });
});
