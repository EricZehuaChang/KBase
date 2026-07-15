<script setup lang="ts">
// 统一页头（G 页面级细化）：标题 + 说明 + 右侧操作区。对标 Vben v5/
// Soybean 的页面骨架——每页顶部一致的标题层级与留白，操作按钮右对齐。
defineProps<{ title: string; subtitle?: string }>();
</script>

<template>
  <div class="mb-5 flex items-start justify-between gap-4">
    <div class="min-w-0">
      <h1 class="text-xl font-semibold tracking-tight">{{ title }}</h1>
      <p v-if="subtitle" class="mt-1 text-sm text-[var(--text-3)]">{{ subtitle }}</p>
    </div>
    <div class="flex shrink-0 items-center gap-2">
      <slot name="actions" />
    </div>
  </div>
</template>
