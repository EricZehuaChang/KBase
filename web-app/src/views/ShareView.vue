<script setup lang="ts">
// 免登录分享问答页（对标 Dify WebApp/FastGPT 免登录窗）：极简收敛——
// 库名 + 输入框 + 流式回答（引用角标/附图/灯箱复用 MessageStream），
// 无模型选择/无库切换/无会话侧栏（模型在建链接侧绑定，token 已绑死库）。
// ?embed=1（widget iframe 场景）时头部收窄。多轮：本页内存内追问（history
// 不落库——分享场景无会话归属）。
import { onMounted, ref } from "vue";
import { useRoute } from "vue-router";
import { SendHorizontal } from "@lucide/vue";
import MessageStream from "@/components/MessageStream.vue";
import { getShareMeta } from "@/lib/api";
import { parseSSE } from "@/lib/sse";
import type { ChatMessage } from "@/composables/useChat";

const route = useRoute();
const token = String(route.params.token || "");
const embed = route.query.embed === "1";

const kbName = ref<string | null>(null);
const invalid = ref(false);
const messages = ref<ChatMessage[]>([]);
const input = ref("");
const busy = ref(false);

onMounted(async () => {
  try {
    kbName.value = (await getShareMeta(token)).kb_name;
  } catch {
    invalid.value = true;    // 链接不存在或已撤销：整页替换为失效提示
  }
});

let seq = 0;

async function ask() {
  const question = input.value.trim();
  if (!question || busy.value) return;
  input.value = "";
  busy.value = true;
  messages.value.push({ id: `u${++seq}`, role: "user", content: question,
                        citations: [], interrupted: false, stopped: false,
                        streaming: false });
  const msg: ChatMessage = { id: `a${++seq}`, role: "assistant", content: "",
                             citations: [], interrupted: false, stopped: false,
                             streaming: true };
  messages.value.push(msg);
  const live = messages.value[messages.value.length - 1];
  try {
    const resp = await fetch(`/api/share/${token}/query`, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ question }),
    });
    if (!resp.ok || !resp.body) throw new Error(`请求失败 (${resp.status})`);
    const gotDone = await parseSSE(resp.body.getReader(), (event, data) => {
      if (event === "citations") {
        // 附图直链改写到 share 公开端点：/api/documents/... 需要登录态，
        // 匿名访客会 401 裂图
        const cites = JSON.parse(data);
        for (const c of cites) {
          for (const img of c.images ?? []) {
            img.url = img.url.replace(
              /^\/api\/documents\/([^/]+)\/images\//,
              `/api/share/${token}/images/$1/`);
          }
        }
        live.citations = cites;
      } else if (event === "token") live.content += data;
    });
    live.interrupted = !gotDone;
  } catch {
    live.interrupted = true;
  } finally {
    live.streaming = false;
    busy.value = false;
  }
}
</script>

<template>
  <div class="flex h-screen w-full flex-col bg-[var(--bg)] text-[var(--text)]">
    <!-- 失效链接：整页提示，不暴露任何系统入口 -->
    <div v-if="invalid" class="flex flex-1 flex-col items-center justify-center gap-2">
      <p class="text-lg font-medium">链接不存在或已失效</p>
      <p class="text-sm text-[var(--text-3)]">请联系为你提供此链接的同事获取新的地址</p>
    </div>

    <template v-else>
      <header
        class="flex shrink-0 items-center gap-2 border-b border-[var(--border)] px-4"
        :class="embed ? 'h-10' : 'h-14'"
      >
        <span class="font-semibold tracking-tight text-[var(--accent-text)]">KBase</span>
        <span class="text-sm text-[var(--text-3)]">·</span>
        <span class="truncate text-sm text-[var(--text-2)]">{{ kbName ?? "…" }}</span>
      </header>

      <main class="min-h-0 flex-1 overflow-y-auto">
        <div v-if="!messages.length"
             class="flex h-full flex-col items-center justify-center gap-1 px-6 text-center">
          <p class="text-lg font-medium">有什么可以帮你？</p>
          <p class="text-sm text-[var(--text-3)]">
            基于「{{ kbName ?? "…" }}」的资料回答，并附引用原文出处
          </p>
        </div>
        <div v-else class="mx-auto max-w-3xl px-4 py-4">
          <MessageStream :messages="messages" />
        </div>
      </main>

      <footer class="shrink-0 px-4 pb-4 pt-1">
        <div class="mx-auto flex max-w-3xl items-end gap-2 rounded-full border border-[var(--border)] bg-[var(--surface)] px-4 py-2 shadow-sm">
          <textarea
            v-model="input"
            rows="1"
            :placeholder="busy ? '回答生成中…' : '输入问题…'"
            :disabled="busy || !kbName"
            class="max-h-32 flex-1 resize-none bg-transparent py-1 text-[15px] leading-[1.6] outline-none placeholder:text-[var(--text-3)]"
            @keydown.enter.exact.prevent="ask"
          />
          <button
            type="button"
            class="rounded-full bg-[var(--accent)] p-2 text-white transition-opacity disabled:opacity-40"
            :disabled="busy || !input.trim()"
            aria-label="发送"
            @click="ask"
          >
            <SendHorizontal class="size-4" />
          </button>
        </div>
        <p class="mx-auto mt-1.5 max-w-3xl text-center text-xs text-[var(--text-3)]">
          回答依据知识库资料并附引用，请以原文为准
        </p>
      </footer>
    </template>
  </div>
</template>
