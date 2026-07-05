<script setup lang="ts">
// 大纲编辑器：节列表行内编辑 title/brief，增删移位走纯函数
// （addSection/removeSection/moveSection，见 generate-utils.ts）——组件本身
// 只负责渲染与 v-model 数组替换，不持有编辑逻辑分支。
import { ChevronUp, ChevronDown, Trash2, Plus } from "@lucide/vue";
import { Button } from "@/components/ui/button";
import { addSection, removeSection, moveSection, type OutlineSection } from "@/lib/generate-utils";

const sections = defineModel<OutlineSection[]>({ required: true });

function updateTitle(i: number, title: string) {
  const next = [...sections.value];
  next[i] = { ...next[i], title };
  sections.value = next;
}

function updateBrief(i: number, brief: string) {
  const next = [...sections.value];
  next[i] = { ...next[i], brief };
  sections.value = next;
}
</script>

<template>
  <div class="flex flex-col gap-3">
    <article
      v-for="(section, i) in sections"
      :key="i"
      class="rounded-[var(--radius-card)] border border-[var(--border)] bg-[var(--surface)] p-3"
    >
      <div class="flex items-start gap-2">
        <span class="mt-2 text-xs text-[var(--text-3)]">{{ i + 1 }}.</span>
        <div class="flex-1 flex flex-col gap-2">
          <input
            :value="section.title"
            type="text"
            placeholder="章节标题"
            aria-label="章节标题"
            class="h-8 w-full rounded-[var(--radius-ctl)] border border-[var(--border)] bg-transparent px-2 text-sm font-medium outline-none focus-visible:border-[var(--accent)]"
            @input="updateTitle(i, ($event.target as HTMLInputElement).value)"
          >
          <textarea
            :value="section.brief"
            placeholder="要点简述"
            aria-label="章节要点简述"
            rows="2"
            class="w-full resize-none rounded-[var(--radius-ctl)] border border-[var(--border)] bg-transparent px-2 py-1.5 text-sm text-[var(--text-2)] outline-none focus-visible:border-[var(--accent)]"
            @input="updateBrief(i, ($event.target as HTMLTextAreaElement).value)"
          />
        </div>
        <div class="flex flex-col gap-0.5">
          <Button
            variant="ghost" size="icon-sm" :disabled="i === 0"
            aria-label="上移" @click="sections = moveSection(sections, i, 'up')"
          >
            <ChevronUp class="size-3.5" />
          </Button>
          <Button
            variant="ghost" size="icon-sm" :disabled="i === sections.length - 1"
            aria-label="下移" @click="sections = moveSection(sections, i, 'down')"
          >
            <ChevronDown class="size-3.5" />
          </Button>
          <Button
            variant="ghost" size="icon-sm"
            aria-label="删除该节" @click="sections = removeSection(sections, i)"
          >
            <Trash2 class="size-3.5" />
          </Button>
        </div>
      </div>
    </article>

    <p v-if="!sections.length" class="py-6 text-center text-sm text-[var(--text-3)]">
      暂无章节，点击下方按钮添加
    </p>

    <Button variant="outline" size="sm" class="self-start" @click="sections = addSection(sections)">
      <Plus class="size-3.5" />
      添加章节
    </Button>
  </div>
</template>
