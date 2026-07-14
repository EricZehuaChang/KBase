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
import { shouldRerunForQuery } from "@/lib/trace-utils";

const route = useRoute();

const kbs = ref<Kb[]>([]);
const kbId = ref<string | undefined>(undefined);
const query = ref("");

// M6-1.5 策略试跑覆盖（不落库）："default"=按该库策略/全局默认，
// "on"/"off"=本次查询强制。用于对比"多路召回/重排开与关"的召回差异。
const keywordOverride = ref("default");
const rerankOverride = ref("default");

function overrideValue(v: string): boolean | undefined {
  return v === "default" ? undefined : v === "on";
}

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
      result.value = await search(kbId.value, query.value.trim(), {
        debug: true, topK: 5,
        useKeyword: overrideValue(keywordOverride.value),
        useRerank: overrideValue(rerankOverride.value),
      });
    }
  } catch (err) {
    // 消息非空由 api.ts req() 统一保证（空响应体时兜底状态码文案）
    error.value = err instanceof Error ? err.message : String(err);
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

// /analysis?q=X → /analysis?q=Y 复用组件实例（router-view 无 :key），
// onMounted 不会重跑——监听 ?q= 变化补上这条路径（如连续点两条消息的
// "检索过程"链接）。守卫条件抽为纯函数 shouldRerunForQuery（vitest 覆盖）。
watch(() => route.query.q, async (q) => {
  if (shouldRerunForQuery(q, query.value)) {
    query.value = q;
    if (kbId.value) await runSearch();
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

      <!-- 策略试跑覆盖（M6-1.5）：仅影响本次查询，不改库配置 -->
      <Select v-model="keywordOverride">
        <SelectTrigger class="w-40" aria-label="多路召回覆盖"><SelectValue /></SelectTrigger>
        <SelectContent>
          <SelectItem value="default">召回·按库策略</SelectItem>
          <SelectItem value="on">召回·强制多路</SelectItem>
          <SelectItem value="off">召回·仅向量</SelectItem>
        </SelectContent>
      </Select>
      <Select v-model="rerankOverride">
        <SelectTrigger class="w-40" aria-label="重排覆盖"><SelectValue /></SelectTrigger>
        <SelectContent>
          <SelectItem value="default">重排·按库策略</SelectItem>
          <SelectItem value="on">重排·强制开</SelectItem>
          <SelectItem value="off">重排·强制关</SelectItem>
        </SelectContent>
      </Select>

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
