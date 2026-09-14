<script setup lang="ts">
// 免登录分享问答页（对标 Dify WebApp/FastGPT 免登录窗）：极简收敛——
// 库名 + 输入框 + 流式回答（引用角标/附图/灯箱复用 MessageStream），
// 无模型选择/无库切换/无会话侧栏（模型在建链接侧绑定，token 已绑死库）。
// ?embed=1（widget iframe 场景）时头部收窄。多轮：本页内存内追问（history
// 不落库——分享场景无会话归属）。
// T10：带口令的链接先弹口令框——口令只存内存，随每次请求走 X-Share-Password
// 头；后端把"口令不对"与"链接失效"分开（401 / 404），这里据此分流：
// 401=留在口令框重试（并提示口令错），404=整页替换成失效提示。
import { onMounted, ref } from "vue";
import { useRoute } from "vue-router";
import { useI18n } from "vue-i18n";
import { SendHorizontal } from "@lucide/vue";
import MessageStream from "@/components/MessageStream.vue";
import { Button } from "@/components/ui/button";
import { Input } from "@/components/ui/input";
import { getShareMeta, SHARE_PASSWORD_HEADER } from "@/lib/api";
import { parseSSE } from "@/lib/sse";
import type { ChatMessage } from "@/composables/useChat";

const route = useRoute();
const { t } = useI18n();
const token = String(route.params.token || "");
const embed = route.query.embed === "1";

const kbName = ref<string | null>(null);
const invalid = ref(false);
const messages = ref<ChatMessage[]>([]);
const input = ref("");
const busy = ref(false);
// T10：口令链接的三种状态——needPassword=显示口令框，password=已提交的口令
// （随请求头带上），passwordWrong=刚提交的口令被拒（401）时给一句提示。
const needPassword = ref(false);
const password = ref("");
const passwordInput = ref("");
const passwordWrong = ref(false);

/** 首屏/重试口令：401 → 弹口令框；404 → 整页失效；200 → 进入问答。
 * 网络异常（后端不可达）也走失效提示：总比留一个永远空白的页面好。 */
async function load() {
  try {
    const meta = await getShareMeta(token, password.value);
    if (meta.state === "ok") {
      // 多库联查链接：头部/空态显示全部库名（访客知道自己在问什么范围）；
      // 单库时 kb_names 长度 1，展示与旧版一致
      kbName.value = meta.kb_names.length
        ? meta.kb_names.join(" · ") : meta.kb_name;
      needPassword.value = false;
      passwordWrong.value = false;
      return;
    }
    if (meta.state === "password") {
      // 已经带过口令还是 401 = 口令不对（首次进入时 password 为空，不提示错误）
      passwordWrong.value = password.value !== "";
      needPassword.value = true;
      return;
    }
    invalid.value = true;  // 链接不存在/已撤销/已过期/次数用尽：整页替换
  } catch {
    invalid.value = true;
  }
}

onMounted(load);

async function submitPassword() {
  const pw = passwordInput.value.trim();
  if (!pw) return;
  password.value = pw;
  await load();
  if (!needPassword.value) passwordInput.value = "";   // 通过后不在框里留明文
}

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
    const headers: Record<string, string> = {
      "Content-Type": "application/json",
    };
    if (password.value) headers[SHARE_PASSWORD_HEADER] = password.value;
    const resp = await fetch(`/api/share/${token}/query`, {
      method: "POST",
      headers,
      body: JSON.stringify({ question }),
    });
    if (resp.status === 401) {
      // 口令在会话中途被改（或首次没带）：退回口令框重试，不留半截回答气泡
      needPassword.value = true;
      passwordWrong.value = true;
      messages.value = messages.value.slice(0, -2);
      return;
    }
    if (!resp.ok || !resp.body) throw new Error(`request failed (${resp.status})`);
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
      <p class="text-lg font-medium">{{ t("share.invalid_title") }}</p>
      <p class="text-sm text-[var(--text-3)]">{{ t("share.invalid_hint") }}</p>
    </div>

    <!-- T10 口令链接（401）：口令框替代问答区，输错只提示不判死链接 -->
    <div
      v-else-if="needPassword"
      class="flex flex-1 flex-col items-center justify-center gap-3 px-6"
    >
      <p class="text-lg font-medium">{{ t("share.password_title") }}</p>
      <p class="text-sm text-[var(--text-3)]">{{ t("share.password_hint") }}</p>
      <div class="flex w-full max-w-xs items-center gap-2">
        <Input
          v-model="passwordInput"
          type="password"
          autocomplete="off"
          :placeholder="t('share.password_placeholder')"
          @keydown.enter="submitPassword"
        />
        <Button :disabled="!passwordInput.trim()" @click="submitPassword">
          {{ t("share.password_submit") }}
        </Button>
      </div>
      <p v-if="passwordWrong" class="text-sm text-[var(--err)]">
        {{ t("share.password_wrong") }}
      </p>
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
          <p class="text-lg font-medium">{{ t("portal.empty.title") }}</p>
          <p class="text-sm text-[var(--text-3)]">
            {{ t("share.based_on", { kb: kbName ?? "…" }) }}
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
            :placeholder="busy ? t('share.generating') : t('share.input_placeholder')"
            :disabled="busy || !kbName"
            class="max-h-32 flex-1 resize-none bg-transparent py-1 text-[15px] leading-[1.6] outline-none placeholder:text-[var(--text-3)]"
            @keydown.enter.exact.prevent="ask"
          />
          <button
            type="button"
            class="rounded-full bg-[var(--accent)] p-2 text-white transition-opacity disabled:opacity-40"
            :disabled="busy || !input.trim()"
            :aria-label="t('portal.chat.send')"
            @click="ask"
          >
            <SendHorizontal class="size-4" />
          </button>
        </div>
        <p class="mx-auto mt-1.5 max-w-3xl text-center text-xs text-[var(--text-3)]">
          {{ t("share.disclaimer") }}
        </p>
      </footer>
    </template>
  </div>
</template>
