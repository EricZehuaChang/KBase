<script setup lang="ts">
// 【使用端】问答页（M5-1 F2 从 src/views/ChatView.vue 移入并原生重构，
// git mv 保留历史）。F1 遗留的耦合已清理：会话侧栏不再 Teleport 进
// PortalShell 提供的 #sidebar-slot 挂载点，改成 SessionSidebar 作为
// 普通子组件直接摆进本页的 flex 布局（PortalShell 那边同步移除了挂载点，
// 见 PortalShell.vue）；KB/模型选择器搬进了 PortalShell 顶栏（跨路由常驻，
// 见 topbar-state.ts），这里只消费选中的 kbId/provider，不再自己维护
// 下拉状态。
import { computed, onBeforeUnmount, ref, watch } from "vue";
import { useRoute } from "vue-router";
import { SendHorizontal, Square } from "@lucide/vue";
import { useI18n } from "vue-i18n";
import { toast } from "vue-sonner";
import { Button } from "@/components/ui/button";
import MessageStream from "@/components/MessageStream.vue";
import CitationDrawer from "@/components/CitationDrawer.vue";
import SessionSidebar from "./SessionSidebar.vue";
import EmptyState from "./EmptyState.vue";
import { kbId, provider, extraKbIds } from "./topbar-state";
import { useSessions } from "./useSessions";
import { type Conversation, type Citation, listMessages, submitFeedback } from "@/lib/api";
import { groupByTime } from "@/lib/chat-utils";
import { useChat } from "@/composables/useChat";

const route = useRoute();
const { t } = useI18n();

const {
  items: conversations, hasMore: hasMoreConvs,
  refresh: refreshConversations, loadMore: loadMoreConversations,
  rename: renameSession, remove: removeSession,
} = useSessions(kbId);

const activeConvId = ref<string | null>(null);
const inputText = ref("");

const { messages, streaming, send, cancel, loadConversation, startNewConversation, convId } =
  useChat(kbId, provider, extraKbIds);

// 切换会话/知识库的取消由 loadConversation/startNewConversation 内部处理；
// 这里兜住离开问答页的场景，避免后台流继续消耗连接。
onBeforeUnmount(cancel);

const conversationGroups = computed(() => groupByTime(conversations.value));

const openCitation = ref<Citation | null>(null);

// 窄屏默认折叠会话侧栏，给消息区腾地方——768px 是常见的平板/桌面断点。
// 只在组件创建时判一次、不跟着 window resize 联动：用户手动展开/收起后，
// 不该被之后的窗口尺寸变化悄悄改回去。
const sidebarCollapsed = ref(typeof window !== "undefined" && window.innerWidth < 768);

async function selectConversation(conv: Conversation) {
  activeConvId.value = conv.id;
  await loadConversation(conv.id, conv.kb_id);
}

function newConversation() {
  activeConvId.value = null;
  startNewConversation();
}

async function handleRename(id: string, title: string) {
  try {
    await renameSession(id, title);
  } catch (err) {
    toast.error(err instanceof Error ? err.message : String(err));
  }
}

async function handleDelete(id: string) {
  try {
    await removeSession(id);
    if (activeConvId.value === id) newConversation();
  } catch (err) {
    toast.error(err instanceof Error ? err.message : String(err));
  }
}

// 发出新一轮问答后会话的 updated_at 会变，需要重新拉一次列表让排序/分组
// （今天/7天内/更早）跟着更新；kbId 切换则是另一码事——整个会话上下文都变了，
// 从"新会话"起步。immediate: true 兜住"PortalShell 的 ensureTopbarLoaded()
// 比本组件先完成、kbId 挂载时已经有值"这种情形——不加 immediate 的话，
// watch 只在后续变化时触发，首次已就绪的 kbId 会被错过，会话列表永远不刷新。
watch(convId, (id) => {
  activeConvId.value = id;
  if (id) void refreshConversations();
});

watch(kbId, async (id) => {
  if (!id) return;
  newConversation();
  await refreshConversations();
}, { immediate: true });

async function handleSend(question?: string) {
  const q = (question ?? inputText.value).trim();
  if (!q || streaming.value || !kbId.value) return;
  inputText.value = "";
  await send(q);
}

function handleKeydown(e: KeyboardEvent) {
  if (e.key === "Enter" && !e.shiftKey) {
    e.preventDefault();
    void handleSend();
  }
}

function handleOpenCitation(index: number, messageId: string) {
  const msg = messages.value.find((m) => m.id === messageId);
  openCitation.value = msg?.citations.find((c) => c.index === index) ?? null;
}

// M6-4 反馈：流式新生成的消息 id 是本地占位（local-N），落库后的真实 id
// 要重新拉一次消息列表才拿得到——点按钮时才解析（懒），不打扰正常聊天流。
// 解析规则：local id 且是最后一条助手消息 → 服务端该会话最后一条 assistant。
async function handleFeedback(messageId: string, rating: 1 | -1) {
  try {
    let realId = messageId;
    if (messageId.startsWith("local-")) {
      if (!convId.value) return;
      const serverMsgs = await listMessages(convId.value);
      const lastAssistant = [...serverMsgs].reverse().find((m) => m.role === "assistant");
      if (!lastAssistant) return;
      realId = lastAssistant.id;
    }
    await submitFeedback(realId, rating);
    toast.success(t(rating === 1 ? "portal.chat.feedback_thanks" : "portal.chat.feedback_recorded"));
  } catch (err) {
    toast.error(err instanceof Error ? err.message : String(err));
  }
}

// "重新提问"只回填输入框，不自动发送——用户点这个按钮通常是想在原问题
// 基础上改两个字再问，不是原样重发（原样重发直接再点一次上一条用户消息
// 复制粘贴也能做到，不需要专门功能）。
function handleReask(question: string) {
  inputText.value = question;
}

const lastQuestion = computed(() => {
  for (let i = messages.value.length - 1; i >= 0; i -= 1) {
    if (messages.value[i].role === "user") return messages.value[i].content;
  }
  return "";
});

// 兼容深链接 ?q=：目前站内没有任何入口会跳到这里带 q 参数（F1 移除了
// "检索过程"跳 /analysis?q= 的旧链路，那是另一条独立的深链接），保留这个
// 读取只是不主动破坏"手动拼 URL 预填问题"这种可能的书签/分享用法，成本
// 接近零。
if (typeof route.query.q === "string" && route.query.q) {
  inputText.value = route.query.q;
}
</script>

<template>
  <div class="flex h-full min-h-0">
    <SessionSidebar
      v-model:collapsed="sidebarCollapsed"
      :groups="conversationGroups"
      :active-id="activeConvId"
      :has-more="hasMoreConvs"
      @new="newConversation"
      @select="selectConversation"
      @rename="handleRename"
      @delete="handleDelete"
      @load-more="loadMoreConversations"
    />

    <div class="flex min-h-0 flex-1 flex-col">
      <!-- 会话内容列居中限宽：满屏宽的长行阅读体验差，~48rem 是可读行宽 -->
      <div class="flex-1 overflow-y-auto px-6 py-6">
        <div class="mx-auto w-full max-w-3xl">
          <EmptyState v-if="messages.length === 0" @pick="handleSend" />
          <MessageStream v-else :messages="messages" @open-citation="handleOpenCitation" @reask="handleReask" @feedback="handleFeedback" />
        </div>
      </div>

      <footer class="shrink-0 px-6 pb-4 pt-2">
        <!-- 卡片式输入区（对标现代 AI 对话产品）：容器聚焦高亮，发送圆钮
        内嵌右下。流式中换"停止生成"而不是禁用发送——停止是该状态下唯一
        有意义的操作，藏起来只留禁用态反而让用户找不到怎么打断。 -->
        <div class="mx-auto w-full max-w-3xl">
          <div
            class="flex items-end gap-2 rounded-2xl border border-[var(--border)] bg-[var(--surface)] p-2 shadow-sm transition-colors focus-within:border-[var(--accent)]"
          >
            <textarea
              v-model="inputText"
              rows="1"
              :disabled="!kbId"
              :placeholder="t('portal.chat.input_placeholder')"
              class="max-h-40 flex-1 resize-none bg-transparent px-2 py-1.5 text-[15px] leading-[1.7] text-[var(--text)] outline-none disabled:opacity-60"
              :aria-label="t('portal.chat.input_label')"
              @keydown="handleKeydown"
            />
            <Button
              v-if="streaming"
              variant="outline"
              size="sm"
              class="shrink-0 rounded-xl"
              @click="cancel"
            >
              <Square class="size-3.5" />
              {{ t("portal.chat.stop") }}
            </Button>
            <Button
              v-else
              size="sm"
              class="shrink-0 rounded-xl"
              :disabled="!inputText.trim() || !kbId"
              :aria-label="t('portal.chat.send')"
              @click="handleSend()"
            >
              <SendHorizontal class="size-4" />
            </Button>
          </div>
          <p class="mt-1.5 text-center text-xs text-[var(--text-3)]">
            {{ t("portal.chat.hint") }}
          </p>
        </div>
      </footer>
    </div>
  </div>

  <CitationDrawer
    :citation="openCitation"
    :query="lastQuestion"
    @close="openCitation = null"
  />
</template>
