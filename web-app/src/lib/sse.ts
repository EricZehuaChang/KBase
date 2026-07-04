// src/lib/sse.ts —— SSE 纯函数解析器。事件内多 data 行以 \n 连接（SSE 规范）；
// 返回 Promise<boolean>：是否收到 done 事件（false = 流中断，调用方展示警示）。
export type SSEHandler = (event: string, data: string) => void;

export async function parseSSE(
  reader: ReadableStreamDefaultReader<Uint8Array>,
  onEvent: SSEHandler,
): Promise<boolean> {
  const dec = new TextDecoder();
  let buf = "", event = "", dataLines: string[] = [], gotDone = false;
  const flush = () => {
    if (!event && dataLines.length === 0) return;
    if (event === "done") gotDone = true;
    else if (event) onEvent(event, dataLines.join("\n"));
    event = ""; dataLines = [];
  };
  const handleLine = (line: string) => {
    if (line.startsWith("event:")) event = line.slice(6).trim();
    else if (line.startsWith("data:")) dataLines.push(line.slice(5).replace(/^ /, ""));
    else if (line === "") flush();
  };
  for (;;) {
    const { done, value } = await reader.read();
    if (done) break;
    buf += dec.decode(value, { stream: true });
    const lines = buf.split(/\r?\n/);
    buf = lines.pop() ?? "";
    lines.forEach(handleLine);
  }
  if (buf) handleLine(buf.replace(/\r$/, ""));
  flush();
  return gotDone;
}
