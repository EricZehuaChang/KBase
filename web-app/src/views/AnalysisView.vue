<script setup lang="ts">
// 检索分析页：KB 下拉 + 查询输入 + 执行按钮 → search(query, {debug:true, topK:5})；
// RetrievalTrace 展示各阶段对比；底部最终 blocks 卡片（text 折叠 200 字）。
// 支持 ?q= 预填（ChatView 的"检索过程"链接携带上次提问跳转到此页），
// 若 KB 已选中则自动执行一次。
import { computed, onMounted, ref, watch } from "vue";
import { useRoute } from "vue-router";
import { Search } from "@lucide/vue";
import { Button } from "@/components/ui/button";
import { Input } from "@/components/ui/input";
import {
  Select, SelectContent, SelectGroup, SelectItem, SelectTrigger, SelectValue,
} from "@/components/ui/select";
import RetrievalTrace from "@/components/RetrievalTrace.vue";
import { listKbs, listDocs, search, type Kb, type SearchResult } from "@/lib/api";

const route = useRoute();

const kbs = ref<Kb[]>([]);
const kbId = ref<string | undefined>(undefined);
const query = ref("");

const loading = ref(false);
const error = ref<string | null>(null);
const result = ref<SearchResult | null>(null);
const kbHasDocs = ref(true);
// 是否已执行过至少一次查询——用来和"尚未查询"的初始空态区分，
// 避免 result 的真值同时承担两种语义（result 为 null 时无法分辨是"未查询"
// 还是"查询过但 KB 无文档，故未调用 search"）。
const searched = ref(false);

const expanded = ref<Set<number>>(new Set());
function toggleExpand(i: number) {
  const next = new Set(expanded.value);
  if (next.has(i)) next.delete(i);
  else next.add(i);
  expanded.value = next;
}
function collapsedText(text: string, i: number): string {
  if (expanded.value.has(i) || text.length <= 200) return text;
  return `${text.slice(0, 200)}…`;
}

async function runSearch() {
  if (!kbId.value || !query.value.trim()) return;
  loading.value = true;
  error.value = null;
  result.value = null;
  kbHasDocs.value = true;
  expanded.value = new Set();
  try {
    const docs = await listDocs(kbId.value);
    kbHasDocs.value = docs.length > 0;
    if (kbHasDocs.value) {
      result.value = await search(kbId.value, query.value.trim(), { debug: true, topK: 5 });
    }
  } catch (err) {
    const message = err instanceof Error ? err.message : String(err);
    // 后端网关错误（如 502）响应体可能为空文本，err.message 会是空字符串——
    // 空字符串在模板 v-if 判断中是假值，需要兜底文案避免错误态"消失"。
    error.value = message || "请求失败，请稍后重试";
  } finally {
    loading.value = false;
    searched.value = true;
  }
}

const isEmpty = computed(() => !loading.value && !error.value && !searched.value);

onMounted(async () => {
  kbs.value = await listKbs();
  if (kbs.value.length) kbId.value = kbs.value[0].id;
  if (typeof route.query.q === "string" && route.query.q) {
    query.value = route.query.q;
  }
  if (kbId.value && query.value.trim()) await runSearch();
});

// 仅当 URL 带 q 且尚未查询过时，KB 加载完成后的这次变化触发自动执行——
// 覆盖 kbs 异步加载慢于 mounted 首次判断的场景。
watch(kbId, async (id) => {
  if (id && query.value.trim() && !searched.value && !loading.value) {
    await runSearch();
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

      <Input
        v-model="query"
        placeholder="输入查询语句"
        class="max-w-md flex-1"
        aria-label="查询输入框"
        @keydown.enter="runSearch"
      />

      <Button :disabled="loading || !kbId || !query.trim()" @click="runSearch">
        <Search class="size-3.5" />
        执行
      </Button>
    </header>

    <div class="flex-1 overflow-y-auto p-6">
      <p v-if="loading" class="text-[var(--text-3)]">检索中…</p>

      <div v-else-if="error" class="rounded-[var(--radius-card)] bg-[var(--err-weak)] p-4 text-[var(--err)]">
        ⚠️ 检索失败：{{ error }}
      </div>

      <div v-else-if="searched && !kbHasDocs" class="py-12 text-center text-[var(--text-3)]">
        该知识库暂无文档，请先前往
        <RouterLink :to="{ path: '/kb', query: { kb: kbId } }" class="text-[var(--accent-text)] underline">
          知识库管理页
        </RouterLink>
        导入文档
      </div>

      <div v-else-if="isEmpty" class="py-12 text-center text-[var(--text-3)]">
        选择知识库并输入查询语句，查看检索过程
      </div>

      <template v-else-if="result">
        <RetrievalTrace v-if="result.trace" :trace="result.trace" />

        <h2 class="mt-8 mb-3 text-sm font-medium text-[var(--text-2)]">最终结果 blocks</h2>
        <div class="flex flex-col gap-3">
          <article
            v-for="(block, i) in result.blocks"
            :key="`${block.doc_id}-${i}`"
            class="rounded-[var(--radius-card)] border border-[var(--border)] bg-[var(--surface)] p-4"
          >
            <div class="flex items-center justify-between">
              <div class="text-sm font-medium">{{ block.doc_name }}</div>
              <span class="rounded-full bg-[var(--surface-2)] px-2 py-0.5 text-xs text-[var(--text-2)]">
                {{ block.score.toFixed(3) }}
              </span>
            </div>
            <div class="mt-1 text-xs text-[var(--text-3)]">{{ block.heading_path }}</div>
            <p class="mt-2 whitespace-pre-wrap text-sm leading-relaxed text-[var(--text-2)]">
              {{ collapsedText(block.text, i) }}
            </p>
            <button
              v-if="block.text.length > 200"
              type="button"
              class="mt-1 text-xs text-[var(--accent-text)] hover:underline"
              @click="toggleExpand(i)"
            >
              {{ expanded.has(i) ? "收起" : "展开全文" }}
            </button>
          </article>
          <p v-if="!result.blocks.length" class="text-sm text-[var(--text-3)]">无匹配结果</p>
        </div>
      </template>
    </div>
  </div>
</template>
