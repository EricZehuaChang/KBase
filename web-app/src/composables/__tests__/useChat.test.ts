import { describe, expect, it, vi, beforeEach, afterEach } from "vitest";
import { ref } from "vue";
import { useChat } from "../useChat";

function sseStreamOf(...chunks: string[]): ReadableStream<Uint8Array> {
  const enc = new TextEncoder();
  return new ReadableStream({
    start(c) {
      chunks.forEach((x) => c.enqueue(enc.encode(x)));
      c.close();
    },
  });
}

describe("useChat", () => {
  const originalFetch = globalThis.fetch;

  beforeEach(() => {
    vi.restoreAllMocks();
  });

  afterEach(() => {
    globalThis.fetch = originalFetch;
  });

  it("中断路径：流未收到 done 事件时，最后一条助手消息追加中断警示", async () => {
    // createConv → POST /api/conversations；queryConv → POST /api/conversations/:id/query
    const fetchMock = vi.fn(async (url: string | URL | Request) => {
      const u = String(url);
      if (u.includes("/api/conversations") && !u.includes("/query")) {
        return new Response(JSON.stringify({ id: "conv-1", kb_id: "kb-1", title: null }), {
          status: 200,
          headers: { "Content-Type": "application/json" },
        });
      }
      if (u.includes("/query")) {
        // 无 done 事件的撕裂流：模拟连接中断
        const body = sseStreamOf(
          'event: citations\r\ndata: [{"index":1,"doc_name":"a.md","heading_path":"h","snippet":"s","score":0.9}]\r\n\r\n' +
            "event: token\r\ndata: 部分回答\r\n\r\n",
        );
        return new Response(body, { status: 200 });
      }
      throw new Error(`unexpected fetch: ${u}`);
    });
    globalThis.fetch = fetchMock as unknown as typeof fetch;

    const kbId = ref<string | undefined>("kb-1");
    const provider = ref<string | undefined>(undefined);
    const { messages, send, streaming } = useChat(kbId, provider);

    await send("测试问题");

    expect(streaming.value).toBe(false);
    const last = messages.value[messages.value.length - 1];
    expect(last.role).toBe("assistant");
    expect(last.interrupted).toBe(true);
    expect(last.content).toContain("部分回答");
    expect(last.content).toContain("回答中断");
    expect(last.citations).toEqual([
      { index: 1, doc_name: "a.md", heading_path: "h", snippet: "s", score: 0.9 },
    ]);
  });

  it("streaming 标志防止重复发送", async () => {
    let resolveFetch: (() => void) | undefined;
    const gate = new Promise<void>((resolve) => {
      resolveFetch = () => resolve();
    });
    const fetchMock = vi.fn(async (url: string | URL | Request) => {
      const u = String(url);
      if (u.includes("/api/conversations") && !u.includes("/query")) {
        return new Response(JSON.stringify({ id: "conv-1", kb_id: "kb-1", title: null }), {
          status: 200,
          headers: { "Content-Type": "application/json" },
        });
      }
      if (u.includes("/query")) {
        await gate;
        return new Response(sseStreamOf("event: done\r\ndata: \r\n\r\n"), { status: 200 });
      }
      throw new Error(`unexpected fetch: ${u}`);
    });
    globalThis.fetch = fetchMock as unknown as typeof fetch;

    const kbId = ref<string | undefined>("kb-1");
    const provider = ref<string | undefined>(undefined);
    const { messages, send, streaming } = useChat(kbId, provider);

    const first = send("第一个问题");
    expect(streaming.value).toBe(true);
    const second = send("第二个问题（应被忽略）");

    resolveFetch?.();
    await Promise.all([first, second]);

    // 只应有一轮用户+助手消息（第二次调用被 streaming 守卫拦截，未 push）
    expect(messages.value.filter((m) => m.role === "user")).toHaveLength(1);
  });
});
