<script setup lang="ts">
// 引用抽屉：右侧滑出 380px 固定面板，展示当前引用的 doc_name/heading_path/
// snippet（snippet 用 <mark> 高亮命中提问词元，highlightSegments 纯函数切分，
// 逐段渲染——不经 v-html）与相关度分数。"查看文档全文"打开 Dialog 展示
// getDocContent 全文，并在 heading_path 首次出现处高亮定位。
import { computed, ref, watch } from "vue";
import { X, FileText } from "@lucide/vue";
import {
  Dialog, DialogContent, DialogHeader, DialogTitle, DialogDescription,
} from "@/components/ui/dialog";
import { Button } from "@/components/ui/button";
import { getDocContent, type Citation, type DocumentContent } from "@/lib/api";
import { highlightSegments } from "@/lib/chat-utils";

const props = defineProps<{
  citation: Citation | null;
  query: string;
}>();
const emit = defineEmits<{ close: [] }>();

const fullTextOpen = ref(false);
const fullTextLoading = ref(false);
const fullTextError = ref<string | null>(null);
const docContent = ref<DocumentContent | null>(null);

const snippetSegments = computed(() =>
  props.citation ? highlightSegments(props.citation.snippet, props.query) : [],
);

// 全文中定位 heading_path 末段（最贴近具体小节的标题），首次出现处高亮。
// 分隔符是后端拼接用的字面量 " > "（chunkers/structure.py），不能按单字符
// >、/ 切——标题自身可能含这些字符（如 "A/B 测试"）。
const headingNeedle = computed(() => {
  const path = props.citation?.heading_path ?? "";
  const parts = path.split(" > ").map((p) => p.trim()).filter(Boolean);
  return parts.length ? parts[parts.length - 1] : path;
});

function fullTextSegments(markdown: string) {
  if (!headingNeedle.value) return [{ type: "text" as const, text: markdown }];
  const idx = markdown.indexOf(headingNeedle.value);
  if (idx < 0) return [{ type: "text" as const, text: markdown }];
  return [
    { type: "text" as const, text: markdown.slice(0, idx) },
    { type: "highlight" as const, text: markdown.slice(idx, idx + headingNeedle.value.length) },
    { type: "text" as const, text: markdown.slice(idx + headingNeedle.value.length) },
  ];
}

async function openFullText() {
  // 旧会话历史里的 citations JSON 无 doc_id（M2 中期才加入载荷）——按钮已按
  // v-if 隐藏，这里再兜一层防御。
  const docId = props.citation?.doc_id;
  if (!docId) return;
  fullTextOpen.value = true;
  fullTextLoading.value = true;
  fullTextError.value = null;
  try {
    docContent.value = await getDocContent(docId);
  } catch (err) {
    fullTextError.value = err instanceof Error ? err.message : String(err);
  } finally {
    fullTextLoading.value = false;
  }
}

// 切换引用时全文预览状态归零，避免残留上一篇文档内容。
watch(() => props.citation, () => {
  fullTextOpen.value = false;
  docContent.value = null;
  fullTextError.value = null;
});
</script>

<template>
  <aside
    v-if="citation"
    class="fixed top-0 right-0 z-40 flex h-screen w-[380px] flex-col border-l border-[var(--border)] bg-[var(--surface)]"
    :style="{ boxShadow: 'var(--shadow-drawer)' }"
    aria-label="引用详情"
  >
    <header class="flex items-center justify-between border-b border-[var(--border)] px-4 py-3">
      <h2 class="text-sm font-medium">引用 [{{ citation.index }}]</h2>
      <Button variant="ghost" size="icon-sm" aria-label="关闭引用抽屉" @click="emit('close')">
        <X class="size-4" />
      </Button>
    </header>

    <div class="flex-1 overflow-y-auto px-4 py-4">
      <div class="text-sm font-medium text-[var(--text)]">{{ citation.doc_name }}</div>
      <div class="mt-1 text-xs text-[var(--text-3)]">{{ citation.heading_path }}</div>

      <p class="mt-3 whitespace-pre-wrap text-sm leading-relaxed text-[var(--text-2)]">
        <template v-for="(seg, i) in snippetSegments" :key="i">
          <mark
            v-if="seg.type === 'highlight'"
            class="rounded-sm bg-[var(--accent-weak)] text-[var(--accent-text)]"
          >{{ seg.text }}</mark>
          <template v-else>{{ seg.text }}</template>
        </template>
      </p>

      <div class="mt-3 flex items-center gap-2 text-xs text-[var(--text-3)]">
        <span>相关度</span>
        <span class="rounded-full bg-[var(--surface-2)] px-2 py-0.5 font-medium text-[var(--text-2)]">
          {{ citation.score.toFixed(3) }}
        </span>
      </div>

      <Button
        v-if="citation.doc_id"
        variant="outline"
        size="sm"
        class="mt-4"
        @click="openFullText"
      >
        <FileText class="size-3.5" />
        查看文档全文
      </Button>
    </div>
  </aside>

  <Dialog v-model:open="fullTextOpen">
    <DialogContent class="max-h-[80vh] max-w-2xl overflow-hidden">
      <DialogHeader>
        <DialogTitle>{{ citation?.doc_name }}</DialogTitle>
        <DialogDescription>{{ citation?.heading_path }}</DialogDescription>
      </DialogHeader>
      <div class="max-h-[60vh] overflow-y-auto text-sm leading-relaxed">
        <p v-if="fullTextLoading" class="text-[var(--text-3)]">加载中…</p>
        <p v-else-if="fullTextError" class="text-[var(--err)]">⚠️ {{ fullTextError }}</p>
        <pre v-else-if="docContent" class="whitespace-pre-wrap font-sans">
          <template v-for="(seg, i) in fullTextSegments(docContent.markdown)" :key="i">
            <mark
              v-if="seg.type === 'highlight'"
              class="rounded-sm bg-[var(--accent-weak)] text-[var(--accent-text)]"
            >{{ seg.text }}</mark>
            <template v-else>{{ seg.text }}</template>
          </template>
        </pre>
      </div>
    </DialogContent>
  </Dialog>
</template>
