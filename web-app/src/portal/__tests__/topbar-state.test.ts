// 顶栏加载闩锁回归：应用启动瞬间路由占位会在未登录时提前触发一次
// ensureTopbarLoaded（401 失败）——失败必须回滚闩锁，登录后的再次调用
// 要能真正拉到数据（此前闩锁不回滚，首登用户面对空知识库下拉）。
import { beforeEach, describe, expect, it, vi } from "vitest";

const listKbs = vi.fn();
const listProviders = vi.fn();
vi.mock("@/lib/api", () => ({
  listKbs: (...a: unknown[]) => listKbs(...a),
  listProviders: (...a: unknown[]) => listProviders(...a),
}));

describe("ensureTopbarLoaded 失败回滚闩锁", () => {
  beforeEach(() => {
    vi.resetModules();
    listKbs.mockReset();
    listProviders.mockReset();
  });

  it("首调 401 失败后，再次调用能重新加载（不被闩锁短路）", async () => {
    const mod = await import("../topbar-state");
    // 第一次：未登录 401（启动期提前触发的场景）
    listKbs.mockRejectedValueOnce(new Error("未认证"));
    await mod.ensureTopbarLoaded();          // 不抛（内部吞掉并回滚闩锁）
    expect(mod.kbs.value).toEqual([]);

    // 第二次：已登录，正常返回
    listKbs.mockResolvedValueOnce([{ id: "kb1", name: "演示知识库" }]);
    listProviders.mockResolvedValueOnce({ active: "qwen", providers: ["qwen"] });
    await mod.ensureTopbarLoaded();
    expect(mod.kbs.value.map((k) => k.id)).toEqual(["kb1"]);
    expect(mod.kbId.value).toBe("kb1");
    expect(mod.provider.value).toBe("qwen");
  });

  it("成功后闩锁生效：后续调用不重复请求", async () => {
    const mod = await import("../topbar-state");
    listKbs.mockResolvedValue([{ id: "kb1", name: "库" }]);
    listProviders.mockResolvedValue({ active: null, providers: [] });
    await mod.ensureTopbarLoaded();
    await mod.ensureTopbarLoaded();
    expect(listKbs).toHaveBeenCalledTimes(1);
  });
});
