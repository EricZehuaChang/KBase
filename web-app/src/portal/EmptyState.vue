<script setup lang="ts">
// 【使用端】问答页空会话状态：新会话/新用户看到的第一屏。纯展示 + 点击
// 转发，不持有状态——快捷问题文案集中在 quick-questions.ts（含选题依据
// 注释），这里只管渲染和把点击事件交给父组件（ChatHome 决定"点了就发送"
// 这件事怎么做，本组件不关心 kbId/发送逻辑）。
import { MessageSquare } from "@lucide/vue";
import { QUICK_QUESTIONS } from "./quick-questions";

const emit = defineEmits<{ pick: [question: string] }>();
</script>

<template>
  <div class="flex h-full flex-col items-center justify-center gap-6 px-6 text-center">
    <div class="flex flex-col items-center gap-2">
      <MessageSquare class="size-8 text-[var(--text-3)]" />
      <h2 class="text-lg font-medium text-[var(--text)]">有什么可以帮你？</h2>
      <p class="text-sm text-[var(--text-3)]">提出一个问题，开始新的对话</p>
    </div>

    <div class="flex w-full max-w-lg flex-col gap-2">
      <button
        v-for="q in QUICK_QUESTIONS"
        :key="q.id"
        type="button"
        class="rounded-[var(--radius-ctl)] border border-[var(--border)] bg-[var(--surface)] px-4 py-2.5 text-left text-sm text-[var(--text-2)] transition-colors hover:border-[var(--accent)] hover:text-[var(--text)]"
        @click="emit('pick', q.text)"
      >
        {{ q.text }}
      </button>
    </div>
  </div>
</template>
