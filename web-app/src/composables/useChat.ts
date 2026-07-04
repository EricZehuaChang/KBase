// src/composables/useChat.ts —— 问答发送流程组合函数。
// 无会话则自动建会话；streaming 标志防止重复发送；parseSSE 的 citations/token
// 事件分别写入当前助手消息；中断（parseSSE 返回 false）追加中断警示；HTTP
// 非 2xx 错误把错误文本写入助手消息（⚠️ 前缀）。
import { ref, type Ref } from "vue";
import { createConv, listMessages, queryConv, type Citation, type Message } from "@/lib/api";
import { parseSSE } from "@/lib/sse";

export interface ChatMessage {
  id: string;
  role: "user" | "assistant";
  content: string;
  citations: Citation[];
  interrupted: boolean;
  streaming: boolean;
}

let localId = 0;
function nextId(): string {
  localId += 1;
  return `local-${localId}`;
}

function fromServerMessage(m: Message): ChatMessage {
  let citations: Citation[] = [];
  if (m.citations) {
    try {
      citations = JSON.parse(m.citations) as Citation[];
    } catch {
      citations = [];
    }
  }
  return {
    id: m.id,
    role: m.role,
    content: m.content,
    citations,
    interrupted: false,
    streaming: false,
  };
}

export function useChat(kbId: Ref<string | undefined>, provider: Ref<string | undefined>) {
  const convId = ref<string | null>(null);
  const messages = ref<ChatMessage[]>([]);
  const streaming = ref(false);

  async function loadConversation(id: string, kb: string) {
    convId.value = id;
    const history = await listMessages(id);
    messages.value = history.map(fromServerMessage);
    void kb; // kb 由调用方切换会话时一并管理，这里不重复请求
  }

  function startNewConversation() {
    convId.value = null;
    messages.value = [];
  }

  async function send(question: string): Promise<void> {
    const trimmed = question.trim();
    if (!trimmed || streaming.value) return;
    if (!kbId.value) return;

    streaming.value = true;
    const userMsg: ChatMessage = {
      id: nextId(), role: "user", content: trimmed,
      citations: [], interrupted: false, streaming: false,
    };
    const assistantMsg: ChatMessage = {
      id: nextId(), role: "assistant", content: "",
      citations: [], interrupted: false, streaming: true,
    };
    messages.value.push(userMsg, assistantMsg);

    try {
      if (!convId.value) {
        const conv = await createConv(kbId.value);
        convId.value = conv.id;
      }
      const res = await queryConv(convId.value, {
        question: trimmed,
        provider: provider.value ?? null,
        top_k: 5,
      });
      if (!res.ok) {
        const text = await res.text();
        let detail: string | undefined;
        try {
          detail = JSON.parse(text)?.detail;
        } catch {
          // 非 JSON 响应体，原样兜底
        }
        assistantMsg.content = `⚠️ ${detail ?? text}`;
        assistantMsg.streaming = false;
        return;
      }
      if (!res.body) {
        assistantMsg.content = "⚠️ 响应无数据流";
        assistantMsg.streaming = false;
        return;
      }
      const reader = res.body.getReader();
      const gotDone = await parseSSE(reader, (event, data) => {
        if (event === "citations") {
          try {
            assistantMsg.citations = JSON.parse(data) as Citation[];
          } catch {
            assistantMsg.citations = [];
          }
        } else if (event === "token") {
          assistantMsg.content += data;
        }
      });
      if (!gotDone) {
        assistantMsg.interrupted = true;
        assistantMsg.content += "\n\n⚠️ 回答中断，请重试";
      }
    } catch (err) {
      assistantMsg.content = `⚠️ ${err instanceof Error ? err.message : String(err)}`;
    } finally {
      assistantMsg.streaming = false;
      streaming.value = false;
    }
  }

  return { convId, messages, streaming, send, loadConversation, startNewConversation };
}
