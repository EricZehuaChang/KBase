import { describe, expect, it, vi, beforeEach, afterEach } from "vitest";
import { defineComponent, h, nextTick, ref } from "vue";
import { mount } from "@vue/test-utils";
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

/** 可手动推帧、保持打开的 SSE 流——用于断言"流未结束时 DOM 已更新"。 */
function heldStream() {
  const enc = new TextEncoder();
  let controller!: ReadableStreamDefaultController<Uint8Array>;
  const stream = new ReadableStream<Uint8Array>({
    start(c) {
      controller = c;
    },
  });
  return {
    stream,
    push: (s: string) => controller.enqueue(enc.encode(s)),
    close: () => controller.close(),
  };
}

function convFetchMock(queryResponse: () => Response) {
  return vi.fn(async (url: string | URL | Request) => {
    const u = String(url);
    if (u.includes("/api/conversations") && !u.includes("/query")) {
      return new Response(JSON.stringify({ id: "conv-1", kb_id: "kb-1", title: null }), {
        status: 200,
        headers: { "Content-Type": "application/json" },
      });
    }
    if (u.includes("/query")) return queryResponse();
    throw new Error(`unexpected fetch: ${u}`);
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

  it("流式 token 实时驱动 DOM 更新（reactive 代理回归测试）", async () => {
    // 回归背景：send() 曾 push 裸对象后继续改 const 引用，绕过 Vue Proxy
    // setter——DOM 整场停在"思考中…"，答案在流结束才整段弹出。
    // 本测试在【流仍未结束】时断言首个 token 已进 DOM，旧实现必然超时失败。
    const held = heldStream();
    globalThis.fetch = convFetchMock(
      () => new Response(held.stream, { status: 200 }),
    ) as unknown as typeof fetch;

    const Harness = defineComponent({
      setup() {
        const kbId = ref<string | undefined>("kb-1");
        const provider = ref<string | undefined>(undefined);
        const { messages, send } = useChat(kbId, provider);
        return { messages, send };
      },
      render() {
        return h("div", this.messages.map((m) => h("p", { key: m.id }, m.content)));
      },
    });

    const wrapper = mount(Harness);
    const sendPromise = wrapper.vm.send("问题");

    held.push("event: citations\r\ndata: []\r\n\r\n");
    held.push("event: token\r\ndata: 第一段\r\n\r\n");
    // 流保持打开——首个 token 必须已渲染进 DOM
    await vi.waitFor(() => {
      expect(wrapper.text()).toContain("第一段");
    });

    held.push("event: token\r\ndata: 第二段\r\n\r\n");
    held.push("event: done\r\ndata: \r\n\r\n");
    held.close();
    await sendPromise;
    await nextTick();
    expect(wrapper.text()).toContain("第一段第二段");
    wrapper.unmount();
  });

  it("cancel() 中止进行中的流：静默收尾，不写 ⚠️ 警示", async () => {
    const enc = new TextEncoder();
    const fetchMock = vi.fn(async (url: string | URL | Request, init?: RequestInit) => {
      const u = String(url);
      if (u.includes("/api/conversations") && !u.includes("/query")) {
        return new Response(JSON.stringify({ id: "conv-1", kb_id: "kb-1", title: null }), {
          status: 200,
          headers: { "Content-Type": "application/json" },
        });
      }
      if (u.includes("/query")) {
        // 推一帧后保持打开；abort 信号触发时以 AbortError 终止读取，
        // 模拟真实 fetch 中止行为。
        const stream = new ReadableStream<Uint8Array>({
          start(c) {
            c.enqueue(enc.encode("event: token\r\ndata: 前半\r\n\r\n"));
            init?.signal?.addEventListener("abort", () => {
              try {
                c.error(new DOMException("aborted", "AbortError"));
              } catch {
                // 流已关闭则忽略
              }
            });
          },
        });
        return new Response(stream, { status: 200 });
      }
      throw new Error(`unexpected fetch: ${u}`);
    });
    globalThis.fetch = fetchMock as unknown as typeof fetch;

    const kbId = ref<string | undefined>("kb-1");
    const provider = ref<string | undefined>(undefined);
    const { messages, send, streaming, cancel } = useChat(kbId, provider);

    const sendPromise = send("测试问题");
    await vi.waitFor(() => {
      expect(messages.value[messages.value.length - 1].content).toContain("前半");
    });

    cancel();
    await sendPromise;

    const last = messages.value[messages.value.length - 1];
    expect(last.content).toBe("前半");          // 已到内容保留，不拼接任何标记文本
    expect(last.content).not.toContain("⚠️");   // 用户主动取消：无错误警示
    expect(last.stopped).toBe(true);            // 已停止标记：与"服务端中断"区分开
    expect(last.interrupted).toBe(false);
    expect(last.streaming).toBe(false);
    expect(streaming.value).toBe(false);
  });
});
