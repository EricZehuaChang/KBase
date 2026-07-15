// src/composables/useChat.ts —— 问答发送流程组合函数。
// 无会话则自动建会话；streaming 标志防止重复发送；parseSSE 的 citations/token
// 事件分别写入当前助手消息；中断（parseSSE 返回 false）追加中断警示；HTTP
// 非 2xx 错误把错误文本写入助手消息（⚠️ 前缀）；cancel() 中止进行中的流
// （切换会话/知识库/离开页面时调用），AbortError 静默收尾不写警示。
import { reactive, ref, type Ref } from "vue";
import {
  createConv, listMessages, queryConv, handleUnauthorized,
  type Citation, type Message,
} from "@/lib/api";
import { parseSSE } from "@/lib/sse";

export interface ChatMessage {
  id: string;
  role: "user" | "assistant";
  content: string;
  citations: Citation[];
  interrupted: boolean;
  // 用户主动点击"停止生成"（或切会话/离开页面）中止时置位——与 interrupted
  // （服务端流意外掉线）是两种不同语义，UI 分别展示"已停止"/"回答中断，
  // 请重试"。不拼进 content：避免污染复制文本与引用角标解析用到的正文。
  stopped: boolean;
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
    stopped: false,
    streaming: false,
  };
}

function isAbortError(err: unknown): boolean {
  return typeof err === "object" && err !== null
    && (err as { name?: string }).name === "AbortError";
}

export function useChat(kbId: Ref<string | undefined>, provider: Ref<string | undefined>,
                        extraKbIds?: Ref<string[]>) {
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
      citations: [], interrupted: false, stopped: false, streaming: false,
    };
    // 关键：助手消息必须先包成 reactive 代理再 push。直接 push 裸对象后继续
    // 通过 const 引用（裸对象）赋值会绕过 Vue 的 Proxy setter——依赖收集在
    // 代理上、变更发生在裸对象上，流式 token 追加不触发重渲染（DOM 整场停在
    // "思考中…"，流结束才整段弹出）。
    const assistantMsg = reactive<ChatMessage>({
      id: nextId(), role: "assistant", content: "",
      citations: [], interrupted: false, stopped: false, streaming: true,
    });
    messages.value.push(userMsg, assistantMsg);

    try {
      if (!convId.value) {
        // M6-2 多库联合问答：有额外联查库时把主库+联查库一起绑进新会话
        //（服务端逐库做 ACL 校验）；无联查=单库老行为。
        const extra = extraKbIds?.value ?? [];
        const conv = await createConv(
          kbId.value,
          extra.length ? [kbId.value, ...extra] : undefined);
        convId.value = conv.id;
      }
      if (ac.signal.aborted) return;   // createConv 期间被取消：直接收尾
      const res = await queryConv(convId.value, {
        question: trimmed,
        provider: provider.value ?? null,
        top_k: 5,
      }, ac.signal);
      if (!res.ok) {
        // SSE 走手工 fetch，绕过 api.ts 的 req() 401 拦截——这里补上：会话中途
        // 失效（如 Cookie 过期）时同样触发全局登出跳转，而不是只在气泡里留一条
        // ⚠️。有已注册的处理器时页面即将跳登录页，气泡冗余故不再写；无处理器
        // （单测/未注册）时保留气泡兜底，避免错误无声消失。
        if (res.status === 401 && handleUnauthorized()) return;
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
      try {
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
      } finally {
        // 显式释放 reader 锁：abort 会让 reader.read() 以 AbortError 拒绝、
        // parseSSE 的 for(;;) 循环提前抛出跳出——不主动 releaseLock 的话，
        // 这个 reader 理论上会一直"锁着"这个 body 流（虽然 reader 对象很快
        // 被 GC，显式释放更可控，避免连续快速点"停止生成"再发下一问时，
        // 上一个未回收的 reader 干扰新请求的流读取）。正常收完 done 事件
        // 时调用同样安全、幂等。
        reader.releaseLock();
      }
    } catch (err) {
      if (isAbortError(err)) {
        // 用户点击"停止生成"，或切会话/离开页面触发 cancel()：保留已生成
        // 的部分内容，打"已停止"标记（不是错误）。切会话场景下这条消息会
        // 被 loadConversation/startNewConversation 整体丢弃，标记不会被
        // 渲染，无副作用。
        assistantMsg.stopped = true;
      } else {
        assistantMsg.content = `⚠️ ${err instanceof Error ? err.message : String(err)}`;
      }
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
