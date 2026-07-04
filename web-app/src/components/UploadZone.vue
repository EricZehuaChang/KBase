<script setup lang="ts">
// 上传区：拖拽高亮边框 + 点击打开文件选择（multiple）。本组件只负责拿到
// File[] 并 emit 给父组件——父组件（KbView）负责调用 uploadDocs 与插入
// 乐观的 parsing 行，保持本组件纯粹（无网络请求，便于复用/测试）。
import { ref } from "vue";
import { UploadCloud } from "@lucide/vue";

const emit = defineEmits<{ filesSelected: [files: File[]] }>();

const dragging = ref(false);
const fileInput = ref<HTMLInputElement | null>(null);

function openPicker() {
  fileInput.value?.click();
}

function handleDrop(e: DragEvent) {
  dragging.value = false;
  const files = Array.from(e.dataTransfer?.files ?? []);
  if (files.length) emit("filesSelected", files);
}

function handleInputChange(e: Event) {
  const input = e.target as HTMLInputElement;
  const files = Array.from(input.files ?? []);
  if (files.length) emit("filesSelected", files);
  input.value = ""; // 允许连续选择同一文件重新上传
}
</script>

<template>
  <div
    role="button"
    tabindex="0"
    aria-label="上传文档，点击或拖拽文件到此处"
    class="flex cursor-pointer flex-col items-center justify-center gap-2 rounded-[var(--radius-card)] border-2 border-dashed px-6 py-8 text-center transition-colors"
    :class="dragging
      ? 'border-[var(--accent)] bg-[var(--accent-weak)]'
      : 'border-[var(--border-strong)] bg-[var(--surface-2)] hover:border-[var(--accent)]'"
    @click="openPicker"
    @keydown.enter="openPicker"
    @dragover.prevent="dragging = true"
    @dragleave.prevent="dragging = false"
    @drop.prevent="handleDrop"
  >
    <UploadCloud class="size-6 text-[var(--text-3)]" />
    <div class="text-sm text-[var(--text-2)]">
      拖拽文件到此处，或<span class="text-[var(--accent-text)]">点击选择</span>
    </div>
    <input
      ref="fileInput"
      type="file"
      multiple
      class="hidden"
      aria-hidden="true"
      @change="handleInputChange"
    >
  </div>
</template>
