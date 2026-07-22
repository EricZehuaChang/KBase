<script setup lang="ts">
// 检索过程三/四栏对比：稠密路/关键词路/RRF融合/重排（若存在）。各列展示
// chunk_id 短前缀 + 分数（3 位小数）；融合列中双路命中的行加 accent 左边框；
// 重排列标注相对融合名次的变化（rankChanges 纯函数，见 lib/trace-utils）。
import { computed } from "vue";
import { useI18n } from "vue-i18n";
import type { TraceStage } from "@/lib/api";
import { rankChanges, shortChunkId, type RankChange } from "@/lib/trace-utils";

const props = defineProps<{ trace: TraceStage }>();
const { t } = useI18n();

const TOP_N = 10;

function topRows(stage: string): [string, number][] {
  return (props.trace[stage] ?? []).slice(0, TOP_N);
}

const dense = computed(() => topRows("dense"));
const keyword = computed(() => topRows("keyword"));
const fused = computed(() => topRows("fused"));
const reranked = computed(() => topRows("reranked"));

const hasKeyword = computed(() => "keyword" in props.trace);
const hasReranked = computed(() => "reranked" in props.trace);

// 融合列中双路都命中的 chunk_id 集合——加 accent 左边框标记
const bothHit = computed(() => {
  const denseIds = new Set(dense.value.map(([id]) => id));
  const keywordIds = new Set(keyword.value.map(([id]) => id));
  const both = new Set<string>();
  for (const id of denseIds) if (keywordIds.has(id)) both.add(id);
  return both;
});

const changes = computed<Record<string, RankChange>>(() =>
  hasReranked.value ? rankChanges(fused.value, props.trace.reranked ?? []) : {},
);

function changeClass(change: RankChange | undefined): string {
  if (!change) return "text-[var(--text-3)]";
  if (change.startsWith("↑")) return "text-[var(--ok)]";
  if (change.startsWith("↓")) return "text-[var(--err)]";
  if (change === "新进") return "text-[var(--accent-text)]";
  return "text-[var(--text-3)]";
}

// 名次变化标记本地化：内部值 "新进" 映射为当前语言；↑N/↓N 为语言中性直接显示。
function changeText(change: RankChange | undefined): string {
  if (!change) return "—";
  if (change === "新进") return t("retrieval.new_entry");
  return change;
}
</script>

<template>
  <div class="grid gap-4" :class="hasReranked ? 'grid-cols-4' : 'grid-cols-3'">
    <!-- 稠密路 -->
    <section :aria-label="t('retrieval.dense')">
      <h3 class="mb-2 text-sm font-medium text-[var(--text-2)]">{{ t("retrieval.dense_top") }}</h3>
      <ol class="flex flex-col gap-1">
        <li
          v-for="[id, score] in dense"
          :key="id"
          class="flex items-center justify-between rounded-[var(--radius-ctl)] bg-[var(--surface-2)] px-2 py-1.5 font-mono text-xs"
        >
          <span>{{ shortChunkId(id) }}</span>
          <span class="text-[var(--text-2)]">{{ score.toFixed(3) }}</span>
        </li>
        <li v-if="!dense.length" class="text-xs text-[var(--text-3)]">{{ t("retrieval.no_result") }}</li>
      </ol>
    </section>

    <!-- 关键词路 -->
    <section :aria-label="t('retrieval.keyword')">
      <h3 class="mb-2 text-sm font-medium text-[var(--text-2)]">{{ t("retrieval.keyword_top") }}</h3>
      <p v-if="!hasKeyword" class="text-xs text-[var(--text-3)]">{{ t("retrieval.disabled") }}</p>
      <ol v-else class="flex flex-col gap-1">
        <li
          v-for="[id, score] in keyword"
          :key="id"
          class="flex items-center justify-between rounded-[var(--radius-ctl)] bg-[var(--surface-2)] px-2 py-1.5 font-mono text-xs"
        >
          <span>{{ shortChunkId(id) }}</span>
          <span class="text-[var(--text-2)]">{{ score.toFixed(3) }}</span>
        </li>
        <li v-if="hasKeyword && !keyword.length" class="text-xs text-[var(--text-3)]">{{ t("retrieval.no_result") }}</li>
      </ol>
    </section>

    <!-- RRF 融合 -->
    <section :aria-label="t('retrieval.fused')">
      <h3 class="mb-2 text-sm font-medium text-[var(--text-2)]">{{ t("retrieval.fused_top") }}</h3>
      <ol class="flex flex-col gap-1">
        <li
          v-for="[id, score] in fused"
          :key="id"
          class="flex items-center justify-between rounded-[var(--radius-ctl)] bg-[var(--surface-2)] px-2 py-1.5 font-mono text-xs"
          :class="bothHit.has(id) ? 'border-l-2 border-[var(--accent)]' : ''"
        >
          <span>{{ shortChunkId(id) }}</span>
          <span class="text-[var(--text-2)]">{{ score.toFixed(3) }}</span>
        </li>
        <li v-if="!fused.length" class="text-xs text-[var(--text-3)]">{{ t("retrieval.no_result") }}</li>
      </ol>
    </section>

    <!-- 重排 -->
    <section v-if="hasReranked" :aria-label="t('retrieval.reranked')">
      <h3 class="mb-2 text-sm font-medium text-[var(--text-2)]">{{ t("retrieval.reranked_top") }}</h3>
      <ol class="flex flex-col gap-1">
        <li
          v-for="[id, score] in reranked"
          :key="id"
          class="flex items-center justify-between rounded-[var(--radius-ctl)] bg-[var(--surface-2)] px-2 py-1.5 font-mono text-xs"
        >
          <span>{{ shortChunkId(id) }}</span>
          <span class="flex items-center gap-2">
            <span class="text-[var(--text-2)]">{{ score.toFixed(3) }}</span>
            <span :class="changeClass(changes[id])">{{ changeText(changes[id]) }}</span>
          </span>
        </li>
        <li v-if="!reranked.length" class="text-xs text-[var(--text-3)]">{{ t("retrieval.no_result") }}</li>
      </ol>
    </section>
  </div>
</template>
