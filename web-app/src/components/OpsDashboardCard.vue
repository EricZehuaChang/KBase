<script setup lang="ts">
// 运营看板卡片（C）：近 N 天问答量/拒答率概览 + 无答案问题清单。
// 无答案清单是知识缺口的直接信号——运营据此补文档。数据源是审计表聚合
// （GET /api/stats/qa、/api/stats/unanswered），只读，admin 可见。
import { onMounted, ref } from "vue";
import { Badge } from "@/components/ui/badge";
import {
  getQaStats, getUnanswered, getFeedbackStats,
  type QaOverview, type UnansweredItem, type FeedbackStats,
} from "@/lib/api";

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
    <div class="mb-3 font-medium">运营看板（近 7 天）</div>
    <p v-if="error" class="text-sm text-[var(--err)]">⚠️ {{ error }}</p>
    <template v-else-if="overview">
      <div class="flex flex-wrap gap-6">
        <div>
          <div class="text-2xl font-semibold">{{ overview.total }}</div>
          <div class="text-xs text-[var(--text-3)]">问答总数</div>
        </div>
        <div>
          <div class="text-2xl font-semibold">{{ overview.refused }}</div>
          <div class="text-xs text-[var(--text-3)]">无答案</div>
        </div>
        <div>
          <div
            class="text-2xl font-semibold"
            :class="overview.refusal_rate > 0.3 ? 'text-[var(--warn)]' : ''"
          >
            {{ pct(overview.refusal_rate) }}
          </div>
          <div class="text-xs text-[var(--text-3)]">拒答率</div>
        </div>
        <div v-if="feedback">
          <div class="text-2xl font-semibold">
            <span class="text-[var(--ok)]">{{ feedback.up }}</span>
            <span class="mx-1 text-base text-[var(--text-3)]">/</span>
            <span class="text-[var(--err)]">{{ feedback.down }}</span>
          </div>
          <div class="text-xs text-[var(--text-3)]">赞 / 踩（M6-4 反馈）</div>
        </div>
      </div>

      <div class="mt-4">
        <div class="mb-2 text-sm font-medium text-[var(--text-2)]">
          最近无答案问题（知识缺口，建议补充文档）
        </div>
        <p v-if="!unanswered.length" class="text-sm text-[var(--text-3)]">
          暂无——所有问题都得到了回答 👍
        </p>
        <ul v-else class="flex flex-col gap-1.5">
          <li
            v-for="(item, i) in unanswered"
            :key="i"
            class="flex items-center gap-2 text-sm"
          >
            <Badge class="bg-[var(--warn-weak)] text-[var(--warn)]">未答</Badge>
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
          最近差评回答（建议核查文档或检索配置）
        </div>
        <ul class="flex flex-col gap-1.5">
          <li
            v-for="item in feedback.items"
            :key="item.message_id"
            class="text-sm"
          >
            <div class="flex items-center gap-2">
              <Badge class="bg-[var(--err-weak)] text-[var(--err)]">差评</Badge>
              <span class="truncate text-[var(--text-2)]">{{ item.question ?? "（问题缺失）" }}</span>
              <span class="ml-auto shrink-0 text-xs text-[var(--text-3)]">
                {{ item.created_at.slice(0, 16).replace("T", " ") }}
              </span>
            </div>
            <div v-if="item.note" class="mt-0.5 pl-12 text-xs text-[var(--text-3)]">
              备注：{{ item.note }}
            </div>
          </li>
        </ul>
      </div>
    </template>
    <p v-else class="text-sm text-[var(--text-3)]">加载中…</p>
  </article>
</template>
