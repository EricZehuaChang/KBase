// src/composables/useChat.ts —— 问答发送流程组合函数。
// 无会话则自动建会话；streaming 标志防止重复发送；parseSSE 的 citations/token
// 事件分别写入当前助手消息；中断（parseSSE 返回 false）追加中断警示；HTTP
// 非 2xx 错误把错误文本写入助手消息（⚠️ 前缀）；cancel() 中止进行中的流
// （切换会话/知识库/离开页面时调用），AbortError 静默收尾不写警示。
import { reactive, ref, type Ref } from "vue";
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

function isAbortError(err: unknown): boolean {
  return typeof err === "object" && err !== null
    && (err as { name?: string }).name === "AbortError";
}

export function useChat(kbId: Ref<string | undefined>, provider: Ref<string | undefined>) {
  const convId = ref<string | null>(null);
  const messages = ref<ChatMessage[]>([]);
  const streaming = ref(false);
  let aborter: AbortController | null = null;

  /** 中止进行中的流式回答。切换会话/知识库、离开页面时调用；
   * fetch/reader 以 AbortError 终止，send() 的 catch 分支静默处理。 */
  function cancel() {
    if (aborter) {
      aborter.abort();
      aborter = null;
    }
    streaming.value = false;
  }

  async function loadConversation(id: string, kb: string) {
    cancel();
    convId.value = id;
    const history = await listMessages(id);
    messages.value = history.map(fromServerMessage);
    void kb; // kb 由调用方切换会话时一并管理，这里不重复请求
  }

  function startNewConversation() {
    cancel();
    convId.value = null;
    messages.value = [];
  }

  async function send(question: string): Promise<void> {
    const trimmed = question.trim();
    if (!trimmed || streaming.value) return;
    if (!kbId.value) return;

    streaming.value = true;
    const ac = new AbortController();
    aborter = ac;
    const userMsg: ChatMessage = {
      id: nextId(), role: "user", content: trimmed,
      citations: [], interrupted: false, streaming: false,
    };
    // 关键：助手消息必须先包成 reactive 代理再 push。直接 push 裸对象后继续
    // 通过 const 引用（裸对象）赋值会绕过 Vue 的 Proxy setter——依赖收集在
    // 代理上、变更发生在裸对象上，流式 token 追加不触发重渲染（DOM 整场停在
    // "思考中…"，流结束才整段弹出）。
    const assistantMsg = reactive<ChatMessage>({
      id: nextId(), role: "assistant", content: "",
      citations: [], interrupted: false, streaming: true,
    });
    messages.value.push(userMsg, assistantMsg);

    try {
      if (!convId.value) {
        const conv = await createConv(kbId.value);
        convId.value = conv.id;
      }
      if (ac.signal.aborted) return;   // createConv 期间被取消：直接收尾
      const res = await queryConv(convId.value, {
        question: trimmed,
        provider: provider.value ?? null,
        top_k: 5,
      }, ac.signal);
      if (!res.ok) {
        const text = await res.text();
        let detail: string | undefined;
        try {
          detail = JSON.parse(text)?.detail;
        } catch {
          // 非 JSON 响应体，原样兜底
        }
        assistantMsg.content = `⚠️ ${detail ?? text}`;
        return;
      }
      if (!res.body) {
        assistantMsg.content = "⚠️ 响应无数据流";
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
      if (!isAbortError(err)) {
        assistantMsg.content = `⚠️ ${err instanceof Error ? err.message : String(err)}`;
      }
      // AbortError = 用户主动切换/离开触发的取消：保留已到内容，静默收尾
    } finally {
      assistantMsg.streaming = false;
      // 仅当当前控制器仍是本次 send 的（未被 cancel/新 send 替换）才复位，
      // 避免旧 send 的收尾误清新 send 的状态。
      if (aborter === ac) {
        aborter = null;
        streaming.value = false;
      }
    }
  }

  return { convId, messages, streaming, send, cancel, loadConversation, startNewConversation };
}
