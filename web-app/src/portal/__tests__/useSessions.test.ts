import { describe, expect, it, vi, beforeEach, afterEach } from "vitest";
import { ref } from "vue";
import { useSessions } from "../useSessions";

function jsonResponse(body: unknown, status = 200): Response {
  return new Response(JSON.stringify(body), {
    status,
    headers: { "Content-Type": "application/json" },
  });
}

describe("useSessions", () => {
  const originalFetch = globalThis.fetch;

  beforeEach(() => {
    vi.restoreAllMocks();
  });

  afterEach(() => {
    globalThis.fetch = originalFetch;
  });

  it("refresh 拉取分页列表", async () => {
    const items = [
      { id: "c1", kb_id: "kb-1", title: "会话一", updated_at: "2026-07-14T00:00:00Z" },
    ];
    globalThis.fetch = vi.fn(async () => jsonResponse({ items, total: 1 })) as unknown as typeof fetch;

    const kbId = ref<string | undefined>("kb-1");
    const { items: sessions, refresh, hasMore } = useSessions(kbId);
    await refresh();

    expect(sessions.value).toEqual(items);
    expect(hasMore.value).toBe(false);
  });

  it("rename 乐观更新：先本地改标题，请求成功后维持新标题", async () => {
    const initial = [{ id: "c1", kb_id: "kb-1", title: "旧标题", updated_at: "2026-07-14T00:00:00Z" }];
    const fetchMock = vi.fn(async (_url: string | URL | Request, init?: RequestInit) => {
      if (init?.method === "PUT") {
        return jsonResponse({ id: "c1", kb_id: "kb-1", title: "新标题" });
      }
      return jsonResponse({ items: initial, total: 1 });
    });
    globalThis.fetch = fetchMock as unknown as typeof fetch;

    const kbId = ref<string | undefined>("kb-1");
    const { items: sessions, refresh, rename } = useSessions(kbId);
    await refresh();

    const renamePromise = rename("c1", "新标题");
    // 乐观更新：请求尚未 resolve 时本地已经是新标题
    expect(sessions.value[0].title).toBe("新标题");
    await renamePromise;
    expect(sessions.value[0].title).toBe("新标题");
  });

  it("rename 失败时把标题精确回滚成旧值，并把错误继续抛给调用方", async () => {
    const initial = [{ id: "c1", kb_id: "kb-1", title: "旧标题", updated_at: "2026-07-14T00:00:00Z" }];
    const fetchMock = vi.fn(async (_url: string | URL | Request, init?: RequestInit) => {
      if (init?.method === "PUT") return jsonResponse({ detail: "会话不存在" }, 404);
      return jsonResponse({ items: initial, total: 1 });
    });
    globalThis.fetch = fetchMock as unknown as typeof fetch;

    const kbId = ref<string | undefined>("kb-1");
    const { items: sessions, refresh, rename } = useSessions(kbId);
    await refresh();

    await expect(rename("c1", "新标题")).rejects.toThrow();
    expect(sessions.value[0].title).toBe("旧标题");   // 回滚
  });

  it("remove 乐观删除：请求成功后该条不再出现", async () => {
    const initial = [
      { id: "c1", kb_id: "kb-1", title: "会话一", updated_at: "2026-07-14T00:00:00Z" },
      { id: "c2", kb_id: "kb-1", title: "会话二", updated_at: "2026-07-13T00:00:00Z" },
    ];
    const fetchMock = vi.fn(async (_url: string | URL | Request, init?: RequestInit) => {
      if (init?.method === "DELETE") return jsonResponse({ ok: true });
      return jsonResponse({ items: initial, total: 2 });
    });
    globalThis.fetch = fetchMock as unknown as typeof fetch;

    const kbId = ref<string | undefined>("kb-1");
    const { items: sessions, refresh, remove } = useSessions(kbId);
    await refresh();

    const removePromise = remove("c1");
    expect(sessions.value.map((s) => s.id)).toEqual(["c2"]);   // 乐观删除立即生效
    await removePromise;
    expect(sessions.value.map((s) => s.id)).toEqual(["c2"]);
  });

  it("remove 失败时把该条插回原位置（不是追加到末尾）", async () => {
    const initial = [
      { id: "c1", kb_id: "kb-1", title: "会话一", updated_at: "2026-07-14T00:00:00Z" },
      { id: "c2", kb_id: "kb-1", title: "会话二", updated_at: "2026-07-13T00:00:00Z" },
      { id: "c3", kb_id: "kb-1", title: "会话三", updated_at: "2026-07-12T00:00:00Z" },
    ];
    const fetchMock = vi.fn(async (_url: string | URL | Request, init?: RequestInit) => {
      if (init?.method === "DELETE") return jsonResponse({ detail: "会话不存在" }, 404);
      return jsonResponse({ items: initial, total: 3 });
    });
    globalThis.fetch = fetchMock as unknown as typeof fetch;

    const kbId = ref<string | undefined>("kb-1");
    const { items: sessions, refresh, remove } = useSessions(kbId);
    await refresh();

    await expect(remove("c2")).rejects.toThrow();
    expect(sessions.value.map((s) => s.id)).toEqual(["c1", "c2", "c3"]);   // 精确回滚到原下标
  });
});
