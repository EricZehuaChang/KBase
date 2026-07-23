// copyToClipboard 兼容性回退（真机踩坑：http 演示机复制按钮无效）。
// 卡住两条路径：安全上下文走 navigator.clipboard；非安全上下文（无
// navigator.clipboard，如 http://IP 部署）回退 execCommand。
import { afterEach, describe, expect, it, vi } from "vitest";

import { copyToClipboard } from "../clipboard";

describe("copyToClipboard", () => {
  afterEach(() => {
    vi.restoreAllMocks();
    vi.unstubAllGlobals();
    delete (document as unknown as { execCommand?: unknown }).execCommand;
  });

  it("安全上下文：调用 navigator.clipboard.writeText", async () => {
    const writeText = vi.fn().mockResolvedValue(undefined);
    vi.stubGlobal("navigator", { clipboard: { writeText } });
    expect(await copyToClipboard("hello")).toBe(true);
    expect(writeText).toHaveBeenCalledWith("hello");
  });

  it("非安全上下文（无 clipboard API）：回退到 execCommand('copy')", async () => {
    vi.stubGlobal("navigator", {}); // http/IP 部署下 navigator.clipboard 为 undefined
    // jsdom 未实现 execCommand（真实浏览器都有），赋值补一个 mock 供回退路径调用
    const exec = vi.fn().mockReturnValue(true);
    (document as unknown as { execCommand: unknown }).execCommand = exec;
    expect(await copyToClipboard("world")).toBe(true);
    expect(exec).toHaveBeenCalledWith("copy");
  });

  it("writeText 抛错 → 返回 false（调用方据此提示失败）", async () => {
    const writeText = vi.fn().mockRejectedValue(new Error("denied"));
    vi.stubGlobal("navigator", { clipboard: { writeText } });
    expect(await copyToClipboard("x")).toBe(false);
  });
});
