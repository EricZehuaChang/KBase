import { describe, expect, it } from "vitest";
import { parseSSE } from "../sse";

function streamOf(...chunks: string[]): ReadableStream<Uint8Array> {
  const enc = new TextEncoder();
  return new ReadableStream({
    start(c) { chunks.forEach(x => c.enqueue(enc.encode(x))); c.close(); },
  });
}

async function collect(stream: ReadableStream<Uint8Array>) {
  const events: { event: string; data: string }[] = [];
  const done = await parseSSE(stream.getReader(), (e, d) => events.push({ event: e, data: d }));
  return { events, done };
}

describe("parseSSE", () => {
  it("多 data 行按 SSE 规范以换行拼接", async () => {
    const { events } = await collect(streamOf(
      "event: token\r\ndata: 第一行\r\ndata: 第二行\r\n\r\n"));
    expect(events[0]).toEqual({ event: "token", data: "第一行\n第二行" });
  });
  it("跨 chunk 撕裂帧可重组（含多字节汉字截断）", async () => {
    const enc = new TextEncoder();
    const bytes = enc.encode("event: token\r\ndata: 汉字流\r\n\r\n");
    const a = bytes.slice(0, 17), b = bytes.slice(17);
    const stream = new ReadableStream<Uint8Array>({
      start(c) { c.enqueue(a); c.enqueue(b); c.close(); },
    });
    const { events } = await collect(stream);
    expect(events[0].data).toBe("汉字流");
  });
  it("done 事件返回 true；无 done 中断返回 false", async () => {
    const ok = await collect(streamOf("event: done\r\ndata: \r\n\r\n"));
    expect(ok.done).toBe(true);
    const cut = await collect(streamOf("event: token\r\ndata: 半截"));
    expect(cut.done).toBe(false);
    expect(cut.events).toEqual([{ event: "token", data: "半截" }]);  // 尾部无空行也 flush
  });
  it("citations JSON 单行完整解析", async () => {
    const { events } = await collect(streamOf(
      'event: citations\r\ndata: [{"index":1}]\r\n\r\n'));
    expect(JSON.parse(events[0].data)).toEqual([{ index: 1 }]);
  });
});
