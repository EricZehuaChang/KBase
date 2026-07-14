<script setup lang="ts">
// 引用抽屉：右侧滑出 380px 固定面板，展示当前引用的 doc_name/heading_path/
// snippet（snippet 用 <mark> 高亮命中提问词元，highlightSegments 纯函数切分，
// 逐段渲染——不经 v-html）与相关度分数。"查看文档全文"打开 Dialog 展示
// getDocContent 全文，并在 heading_path 首次出现处高亮定位。
import { computed, ref, watch } from "vue";
import { X, FileText, Download, Eye } from "@lucide/vue";
import {
  Dialog, DialogContent, DialogHeader, DialogTitle, DialogDescription,
} from "@/components/ui/dialog";
import { Button } from "@/components/ui/button";
import { docOriginalUrl, getDocContent, type Citation, type DocumentContent } from "@/lib/api";
import { highlightSegments, originalPreviewKind } from "@/lib/chat-utils";

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

// ---- 原始文件预览（M5-2 引用定位）----
// pdf → iframe 内联浏览器查看器，带 #page=N 直接跳到引用所在页附近；
// 图片 → <img> 直显。docx/xlsx 等浏览器渲染不了，不出现此按钮（回退
// "查看文档全文"的 Markdown 定位 + 下载原件）。
const previewOpen = ref(false);
const previewKind = computed(() => originalPreviewKind(props.citation?.doc_name));
const previewUrl = computed(() => {
  const docId = props.citation?.doc_id;
  if (!docId) return "";
  return docOriginalUrl(docId, { inline: true, page: props.citation?.page ?? undefined });
});

// 切换引用时全文/原件预览状态归零，避免残留上一篇文档内容。
watch(() => props.citation, () => {
  fullTextOpen.value = false;
  previewOpen.value = false;
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
        <!-- 源文件页码（文本层 PDF 才有）：预览按钮会直接跳到这一页 -->
        <span
          v-if="citation.page"
          class="rounded-full bg-[var(--surface-2)] px-2 py-0.5 font-medium text-[var(--text-2)]"
        >
          第 {{ citation.page }} 页
        </span>
      </div>

      <div v-if="citation.doc_id" class="mt-4 flex flex-wrap items-center gap-2">
        <!-- 原始文件预览（M5-2）：pdf/图片浏览器可原生渲染时才出现；
             pdf 带 #page= 定位到引用所在页附近 -->
        <Button v-if="previewKind" variant="outline" size="sm" @click="previewOpen = true">
          <Eye class="size-3.5" />
          预览原文件{{ citation.page ? `（第${citation.page}页）` : "" }}
        </Button>
        <Button variant="outline" size="sm" @click="openFullText">
          <FileText class="size-3.5" />
          查看文档全文
        </Button>
        <!-- 原始文件下载：识别前的 .docx/.pdf/扫描图原件，浏览器原生下载
             （同源带 Cookie），文件名恢复上传原名 -->
        <Button variant="outline" size="sm" as="a" :href="docOriginalUrl(citation.doc_id)">
          <Download class="size-3.5" />
          下载原文件
        </Button>
      </div>
    </div>
  </aside>

  <!-- 原始文件预览 Dialog（M5-2 引用定位） -->
  <Dialog v-model:open="previewOpen">
    <DialogContent class="max-h-[90vh] max-w-4xl overflow-hidden">
      <DialogHeader>
        <DialogTitle>{{ citation?.doc_name }}</DialogTitle>
        <DialogDescription>
          {{ citation?.page ? `已定位到第 ${citation.page} 页附近` : "原始文件预览" }}
        </DialogDescription>
      </DialogHeader>
      <!-- pdf：浏览器自带查看器（#page=N 跳页）；image：原图直显 -->
      <iframe
        v-if="previewOpen && previewKind === 'pdf'"
        :src="previewUrl"
        class="h-[72vh] w-full rounded-[var(--radius-ctl)] border border-[var(--border)]"
        title="原始文件预览"
      />
      <div v-else-if="previewOpen && previewKind === 'image'" class="max-h-[72vh] overflow-auto">
        <img :src="previewUrl" :alt="citation?.doc_name" class="max-w-full" />
      </div>
    </DialogContent>
  </Dialog>

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
