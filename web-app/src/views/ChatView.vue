<script setup lang="ts">
// 问答页：顶栏 KB+Provider 下拉；中部消息流；底部输入条；侧栏会话列表
// 通过 Teleport 注入 AppShell 的 #sidebar-slot（AppShell 与路由视图不在同一
// 组件树，Teleport 是跨树注入内容的标准 Vue 方案）。
import { computed, onMounted, ref, watch } from "vue";
import { useRoute } from "vue-router";
import { Plus } from "@lucide/vue";
import { Button } from "@/components/ui/button";
import {
  Select, SelectContent, SelectGroup, SelectItem, SelectTrigger, SelectValue,
} from "@/components/ui/select";
import MessageStream from "@/components/MessageStream.vue";
import CitationDrawer from "@/components/CitationDrawer.vue";
import {
  listKbs, listConvs, listProviders, listDocs,
  type Kb, type Conversation, type Citation, type DocumentItem,
} from "@/lib/api";
import { groupByTime } from "@/lib/chat-utils";
import { useChat } from "@/composables/useChat";

const route = useRoute();

const kbs = ref<Kb[]>([]);
const kbId = ref<string | undefined>(undefined);
const providers = ref<string[]>([]);
const provider = ref<string | undefined>(undefined);
const conversations = ref<Conversation[]>([]);
const activeConvId = ref<string | null>(null);
const inputText = ref("");

const { messages, streaming, send, loadConversation, startNewConversation, convId } =
  useChat(kbId, provider);

const conversationGroups = computed(() => groupByTime(conversations.value));

const openCitation = ref<Citation | null>(null);
const openCitationMessageId = ref<string | null>(null);
const lastQuestion = ref("");
// 引用（SSE citations / 会话历史 Message.citations）只带 doc_name，不带
// doc_id —— Generator.citations() 未回传 ContextBlock.doc_id（后端契约缺口，
// 已记录）。这里按当前 KB 的文档列表用文件名反查 doc_id，供"查看文档全文"用。
const docsByName = ref<Map<string, DocumentItem>>(new Map());

async function refreshConversations() {
  if (!kbId.value) return;
  conversations.value = await listConvs(kbId.value);
}

async function refreshDocsIndex() {
  if (!kbId.value) return;
  const docs = await listDocs(kbId.value);
  docsByName.value = new Map(docs.map((d) => [d.filename, d]));
}

async function selectConversation(conv: Conversation) {
  activeConvId.value = conv.id;
  await loadConversation(conv.id, conv.kb_id);
}

function newConversation() {
  activeConvId.value = null;
  startNewConversation();
}

watch(convId, (id) => {
  activeConvId.value = id;
  if (id) void refreshConversations();
});

watch(kbId, async (id) => {
  if (!id) return;
  newConversation();
  await Promise.all([refreshConversations(), refreshDocsIndex()]);
});

async function handleSend() {
  const question = inputText.value;
  if (!question.trim() || streaming.value) return;
  lastQuestion.value = question;
  inputText.value = "";
  await send(question);
}

function handleKeydown(e: KeyboardEvent) {
  if (e.key === "Enter" && !e.shiftKey) {
    e.preventDefault();
    void handleSend();
  }
}

function handleOpenCitation(index: number, messageId: string) {
  const msg = messages.value.find((m) => m.id === messageId);
  const citation = msg?.citations.find((c) => c.index === index) ?? null;
  openCitation.value = citation;
  openCitationMessageId.value = messageId;
}

const openCitationDocId = computed(() =>
  openCitation.value ? docsByName.value.get(openCitation.value.doc_name)?.id ?? null : null,
);

onMounted(async () => {
  kbs.value = await listKbs();
  if (kbs.value.length) kbId.value = kbs.value[0].id;
  const providersResp = await listProviders();
  providers.value = providersResp.providers;
  provider.value = providersResp.active ?? undefined;
  if (typeof route.query.q === "string" && route.query.q) {
    inputText.value = route.query.q;
  }
});
</script>

<template>
  <div class="flex h-full flex-col">
    <header class="flex h-14 shrink-0 items-center gap-3 border-b border-[var(--border)] px-4">
      <Select v-model="kbId">
        <SelectTrigger class="w-48"><SelectValue placeholder="选择知识库" /></SelectTrigger>
        <SelectContent>
          <SelectGroup>
            <SelectItem v-for="kb in kbs" :key="kb.id" :value="kb.id">{{ kb.name }}</SelectItem>
          </SelectGroup>
        </SelectContent>
      </Select>

      <Select v-model="provider">
        <SelectTrigger class="w-48"><SelectValue placeholder="选择模型" /></SelectTrigger>
        <SelectContent>
          <SelectGroup>
            <SelectItem v-for="p in providers" :key="p" :value="p">{{ p }}</SelectItem>
          </SelectGroup>
        </SelectContent>
      </Select>
    </header>

    <div class="flex-1 overflow-y-auto px-6 py-6">
      <MessageStream :messages="messages" @open-citation="handleOpenCitation" />
    </div>

    <footer class="shrink-0 border-t border-[var(--border)] p-4">
      <div class="flex items-end gap-2">
        <textarea
          v-model="inputText"
          rows="1"
          placeholder="输入问题，Enter 发送，Shift+Enter 换行"
          class="max-h-40 flex-1 resize-none rounded-[var(--radius-ctl)] border border-[var(--border)] bg-[var(--surface)] px-3 py-2 text-[15px] leading-[1.7] text-[var(--text)] outline-none focus:border-[var(--accent)]"
          aria-label="问题输入框"
          @keydown="handleKeydown"
        />
        <Button :disabled="streaming || !inputText.trim()" @click="handleSend">发送</Button>
      </div>
    </footer>
  </div>

  <CitationDrawer
    :citation="openCitation"
    :doc-id="openCitationDocId"
    :query="lastQuestion"
    @close="openCitation = null; openCitationMessageId = null"
  />

  <Teleport to="#sidebar-slot">
    <div class="flex flex-col gap-1 py-2">
      <Button variant="outline" size="sm" class="mb-2 justify-start" @click="newConversation">
        <Plus class="size-3.5" />
        新会话
      </Button>

      <div v-for="group in conversationGroups" :key="group.label" class="mb-2">
        <div class="px-2 py-1 text-xs font-medium text-[var(--text-3)]">{{ group.label }}</div>
        <button
          v-for="conv in group.items"
          :key="conv.id"
          type="button"
          class="w-full truncate rounded-[var(--radius-ctl)] px-2 py-1.5 text-left text-sm transition-colors"
          :class="activeConvId === conv.id
            ? 'bg-[var(--accent-weak)] text-[var(--accent-text)]'
            : 'text-[var(--text-2)] hover:bg-[var(--surface-2)]'"
          @click="selectConversation(conv)"
        >
          {{ conv.title ?? "新会话" }}
        </button>
      </div>
    </div>
  </Teleport>
</template>
