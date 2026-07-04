<script setup lang="ts">
// 消息流：用户消息右对齐浅底圆角块；助手消息为流动排版（无气泡），
// 流式中显示"思考中…"占位直到首个字符到达；引用角标 [n] 渲染为可点击
// accent 小圆片（renderWithChips 纯函数切分，逐段渲染，不经 v-html）。
import { computed } from "vue";
import { useRouter } from "vue-router";
import { toast } from "vue-sonner";
import { Copy, ScanSearch } from "@lucide/vue";
import { Button } from "@/components/ui/button";
import { renderWithChips } from "@/lib/chat-utils";
import type { ChatMessage } from "@/composables/useChat";

const props = defineProps<{ messages: ChatMessage[] }>();
const emit = defineEmits<{ openCitation: [index: number, messageId: string] }>();

const router = useRouter();

function segmentsOf(content: string) {
  return renderWithChips(content);
}

function lastQuestionFor(index: number): string {
  for (let i = index - 1; i >= 0; i -= 1) {
    if (props.messages[i].role === "user") return props.messages[i].content;
  }
  return "";
}

async function copyMessage(content: string) {
  try {
    await navigator.clipboard.writeText(content);
    toast.success("已复制到剪贴板");
  } catch {
    toast.error("复制失败，请手动选择文本");
  }
}

function openRetrievalTrace(index: number) {
  const question = lastQuestionFor(index);
  router.push({ path: "/analysis", query: question ? { q: question } : {} });
}

const isEmpty = computed(() => props.messages.length === 0);
</script>

<template>
  <ol class="flex flex-col gap-6" aria-label="对话消息列表">
    <li v-if="isEmpty" class="py-12 text-center text-[var(--text-3)]">
      提出一个问题，开始新的对话
    </li>

    <li
      v-for="(message, index) in messages"
      :key="message.id"
      class="flex"
      :class="message.role === 'user' ? 'justify-end' : 'justify-start'"
    >
      <div
        v-if="message.role === 'user'"
        class="max-w-[70%] rounded-[var(--radius-card)] bg-[var(--surface-2)] px-4 py-2.5 text-[var(--text)]"
      >
        {{ message.content }}
      </div>

      <div v-else class="w-full max-w-[80ch]">
        <div v-if="message.streaming && !message.content" class="text-[var(--text-3)]">
          思考中…
        </div>
        <div v-else class="whitespace-pre-wrap text-[var(--text)]">
          <template v-for="(seg, si) in segmentsOf(message.content)" :key="si">
            <span v-if="seg.type === 'text'">{{ seg.text }}</span>
            <button
              v-else
              type="button"
              class="mx-0.5 inline-flex size-5 items-center justify-center rounded-full bg-[var(--accent-weak)] text-xs font-medium text-[var(--accent-text)] align-middle hover:bg-[var(--accent)] hover:text-[var(--surface)]"
              :aria-label="`查看引用 ${seg.index}`"
              @click="emit('openCitation', seg.index, message.id)"
            >
              {{ seg.index }}
            </button>
          </template>
        </div>

        <div
          v-if="!message.streaming && message.content"
          class="mt-2 flex items-center gap-3 text-sm text-[var(--text-3)]"
        >
          <span v-if="message.citations.length" class="rounded-full bg-[var(--surface-2)] px-2 py-0.5">
            {{ message.citations.length }} 条引用
          </span>
          <Button variant="ghost" size="sm" @click="copyMessage(message.content)">
            <Copy class="size-3.5" />
            复制
          </Button>
          <Button variant="ghost" size="sm" @click="openRetrievalTrace(index)">
            <ScanSearch class="size-3.5" />
            检索过程
          </Button>
        </div>
      </div>
    </li>
  </ol>
</template>
