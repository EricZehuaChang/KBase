<script setup lang="ts">
// 运营看板卡片（C）：近 N 天问答量/拒答率概览 + 无答案问题清单。
// 无答案清单是知识缺口的直接信号——运营据此补文档。数据源是审计表聚合
// （GET /api/stats/qa、/api/stats/unanswered），只读，admin 可见。
import { onMounted, ref } from "vue";
import { useI18n } from "vue-i18n";
import { Badge } from "@/components/ui/badge";
import {
  getQaStats, getUnanswered, getFeedbackStats,
  type QaOverview, type UnansweredItem, type FeedbackStats,
} from "@/lib/api";

const { t } = useI18n();

const overview = ref<QaOverview | null>(null);
const unanswered = ref<UnansweredItem[]>([]);
const feedback = ref<FeedbackStats | null>(null);
const error = ref<string | null>(null);

onMounted(async () => {
  try {
    const [ov, un, fb] = await Promise.all([
      getQaStats(7), getUnanswered(20), getFeedbackStats(20)]);
    overview.value = ov;
    unanswered.value = un.items;
    feedback.value = fb;
  } catch (err) {
    error.value = err instanceof Error ? err.message : String(err);
  }
});

function pct(rate: number): string {
  return `${(rate * 100).toFixed(1)}%`;
}
</script>

<template>
  <article class="rounded-[var(--radius-card)] border border-[var(--border)] bg-[var(--surface)] p-4">
    <div class="mb-3 font-medium">{{ t("ops.title") }}</div>
    <p v-if="error" class="text-sm text-[var(--err)]">⚠️ {{ error }}</p>
    <template v-else-if="overview">
      <div class="flex flex-wrap gap-6">
        <div>
          <div class="text-2xl font-semibold">{{ overview.total }}</div>
          <div class="text-xs text-[var(--text-3)]">{{ t("ops.total") }}</div>
        </div>
        <div>
          <div class="text-2xl font-semibold">{{ overview.refused }}</div>
          <div class="text-xs text-[var(--text-3)]">{{ t("ops.refused") }}</div>
        </div>
        <div>
          <div
            class="text-2xl font-semibold"
            :class="overview.refusal_rate > 0.3 ? 'text-[var(--warn)]' : ''"
          >
            {{ pct(overview.refusal_rate) }}
          </div>
          <div class="text-xs text-[var(--text-3)]">{{ t("ops.refusal_rate") }}</div>
        </div>
        <div v-if="feedback">
          <div class="text-2xl font-semibold">
            <span class="text-[var(--ok)]">{{ feedback.up }}</span>
            <span class="mx-1 text-base text-[var(--text-3)]">/</span>
            <span class="text-[var(--err)]">{{ feedback.down }}</span>
          </div>
          <div class="text-xs text-[var(--text-3)]">{{ t("ops.feedback") }}</div>
        </div>
      </div>

      <div class="mt-4">
        <div class="mb-2 text-sm font-medium text-[var(--text-2)]">
          {{ t("ops.unanswered_title") }}
        </div>
        <p v-if="!unanswered.length" class="text-sm text-[var(--text-3)]">
          {{ t("ops.no_unanswered") }}
        </p>
        <ul v-else class="flex flex-col gap-1.5">
          <li
            v-for="(item, i) in unanswered"
            :key="i"
            class="flex items-center gap-2 text-sm"
          >
            <Badge class="bg-[var(--warn-weak)] text-[var(--warn)]">{{ t("ops.unanswered_badge") }}</Badge>
            <span class="truncate text-[var(--text-2)]">{{ item.question }}</span>
            <span class="ml-auto shrink-0 text-xs text-[var(--text-3)]">
              {{ item.ts.slice(0, 16).replace("T", " ") }}
            </span>
          </li>
        </ul>
      </div>

      <!-- M6-4 差评清单：与无答案清单互补——拒答=答不上，差评=答砸了 -->
      <div v-if="feedback?.items.length" class="mt-4">
        <div class="mb-2 text-sm font-medium text-[var(--text-2)]">
          {{ t("ops.downvote_title") }}
        </div>
        <ul class="flex flex-col gap-1.5">
          <li
            v-for="item in feedback.items"
            :key="item.message_id"
            class="text-sm"
          >
            <div class="flex items-center gap-2">
              <Badge class="bg-[var(--err-weak)] text-[var(--err)]">{{ t("ops.downvote_badge") }}</Badge>
              <span class="truncate text-[var(--text-2)]">{{ item.question ?? t("ops.question_missing") }}</span>
              <span class="ml-auto shrink-0 text-xs text-[var(--text-3)]">
                {{ item.created_at.slice(0, 16).replace("T", " ") }}
              </span>
            </div>
            <div v-if="item.note" class="mt-0.5 pl-12 text-xs text-[var(--text-3)]">
              {{ t("ops.note", { note: item.note }) }}
            </div>
          </li>
        </ul>
      </div>
    </template>
    <p v-else class="text-sm text-[var(--text-3)]">{{ t("common.loading") }}</p>
  </article>
</template>
