<script setup lang="ts">
// 运营看板卡片（C）：近 N 天问答量/拒答率概览 + 无答案问题清单
// + T12 问答归因（分桶 / 下钻 / CSV 导出）。
//
// 无答案清单出自审计表（100 字问题前缀），归因出自 qa_outcomes（完整问题 +
// 命中数/最高分 + 桶）。两套并存不是重复：审计是安全口径，归因是运营口径
// （能按桶聚合、能下钻到当次引用、能导出）。
//
// **归因数据只能从上线后开始积累**：历史问答当时没记命中数/最高分，无法回填
// （重跑历史问题拿到的是今天的检索结果，不是当时的事故现场，混进来只会污染
// 统计）。所以这里上线初期只会看到很少的行，第一份有意义的报告要等数据攒够
// （周量级）——界面上写明了这一点，避免被读成"功能没生效"。
import { computed, onMounted, ref } from "vue";
import { useI18n } from "vue-i18n";
import { ChevronDown, ChevronRight, FileDown, Loader2 } from "@lucide/vue";
import { Badge } from "@/components/ui/badge";
import {
  getQaStats, getUnanswered, getFeedbackStats, getOutcome, listKbs, listOutcomes,
  outcomeExportUrl,
  type Kb, type OutcomeBucket, type OutcomeDetail, type OutcomeItem,
  type OutcomePage, type QaOverview, type UnansweredItem, type FeedbackStats,
} from "@/lib/api";

const { t } = useI18n();

const overview = ref<QaOverview | null>(null);
const unanswered = ref<UnansweredItem[]>([]);
const feedback = ref<FeedbackStats | null>(null);
const error = ref<string | null>(null);

// ---- T12 归因 ----
const outcomes = ref<OutcomePage | null>(null);
const outcomesLoading = ref(false);
const outcomeError = ref<string | null>(null);
const kbs = ref<Kb[]>([]);
const bucket = ref<string>("");
const channel = ref<string>("");
const kbId = ref<string>("");
const openId = ref<string>("");
const detail = ref<OutcomeDetail | null>(null);
const detailLoading = ref(false);

// 桶的展示顺序与配色：先"能补的知识缺口"（空检索/低于阈值），再安全事件，
// 最后正常作答——与后端 BUCKETS 的顺序一致，看板从上到下就是运营的处理顺序。
const BUCKET_CLASS: Record<string, string> = {
  empty_retrieval: "bg-[var(--warn-weak)] text-[var(--warn)]",
  below_threshold: "bg-[var(--warn-weak)] text-[var(--warn)]",
  scope_denied: "bg-[var(--err-weak)] text-[var(--err)]",
  answered: "bg-[var(--ok-weak)] text-[var(--ok)]",
};

// 渠道取值由各入口写死（web/share/v1/feishu/mcp）；这里下拉的清单是"已知渠道"，
// 选不到的新渠道仍会出现在行里，不做前端白名单过滤（后端不限制该参数）。
const CHANNELS = ["web", "share", "v1", "feishu"];

const bucketKeys = computed<OutcomeBucket[]>(
  () => outcomes.value?.buckets_known ?? [],
);

function bucketLabel(b: string): string {
  // t() 查不到时回落原始码：后端将来新增桶不会渲染成空白
  const key = `ops.bucket.${b}`;
  const translated = t(key);
  return translated !== key ? translated : b;
}

function bucketClass(b: string): string {
  return BUCKET_CLASS[b] ?? "bg-[var(--surface-2)] text-[var(--text-2)]";
}

function formatTime(iso: string | null): string {
  if (!iso) return "";
  // 后端写的是 naive UTC（utcnow），补 Z 再本地化；解析失败原样显示
  const d = new Date(iso.endsWith("Z") ? iso : `${iso}Z`);
  return Number.isNaN(d.getTime()) ? iso : d.toLocaleString();
}

function pct(rate: number): string {
  return `${(rate * 100).toFixed(1)}%`;
}

/** 选桶 = 过滤清单（分布本身不跟着变：后端 buckets 不叠加 bucket 过滤） */
function pickBucket(b: string): void {
  bucket.value = bucket.value === b ? "" : b;
  openId.value = "";
  detail.value = null;
  void loadOutcomes();
}

/** 导出与当前筛选同参：导出的 CSV 与面板上看到的必须是同一批行 */
function exportUrl(): string {
  return outcomeExportUrl({ bucket: bucket.value || undefined,
                            channel: channel.value || undefined,
                            kbId: kbId.value || undefined, limit: 200 });
}

async function loadOutcomes(): Promise<void> {
  outcomesLoading.value = true;
  outcomeError.value = null;
  try {
    outcomes.value = await listOutcomes({
      bucket: bucket.value || undefined,
      channel: channel.value || undefined,
      kbId: kbId.value || undefined,
      limit: 20,
    });
  } catch (err) {
    outcomeError.value = err instanceof Error ? err.message : String(err);
  } finally {
    outcomesLoading.value = false;
  }
}

/** 行展开 = 下钻进那一轮问答：完整问题 + 答案原文 + 当次 citations */
async function toggle(row: OutcomeItem): Promise<void> {
  if (openId.value === row.id) {
    openId.value = "";
    detail.value = null;
    return;
  }
  openId.value = row.id;
  detail.value = null;
  detailLoading.value = true;
  try {
    detail.value = await getOutcome(row.id);
  } catch (err) {
    outcomeError.value = err instanceof Error ? err.message : String(err);
  } finally {
    detailLoading.value = false;
  }
}

onMounted(async () => {
  try {
    const [ov, un, fb, out, kbList] = await Promise.all([
      getQaStats(7), getUnanswered(20), getFeedbackStats(20),
      listOutcomes({ limit: 20 }), listKbs()]);
    overview.value = ov;
    unanswered.value = un.items;
    feedback.value = fb;
    outcomes.value = out;
    kbs.value = kbList;
  } catch (err) {
    error.value = err instanceof Error ? err.message : String(err);
  }
});
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

      <!-- T12 问答归因：分桶 → 点桶过滤 → 点行下钻 → 导出 CSV -->
      <div class="mt-6 border-t border-[var(--border)] pt-4" data-tour="ops-attribution">
        <div class="mb-1 flex flex-wrap items-center gap-2">
          <span class="text-sm font-medium text-[var(--text-2)]">
            {{ t("ops.attr_title") }}
          </span>
          <span v-if="outcomes" class="text-xs text-[var(--text-3)]">
            {{ t("ops.attr_total", { n: outcomes.total }) }}
          </span>
          <a
            v-if="outcomes && outcomes.total"
            class="ml-auto inline-flex items-center gap-1.5 text-xs text-[var(--accent-text)] hover:underline"
            :href="exportUrl()"
            download
          >
            <FileDown class="size-3.5" />{{ t("ops.attr_export") }}
          </a>
        </div>
        <!-- 口径说明必须常驻：上线初期的少量数据是正常的，不是功能没生效 -->
        <p class="mb-3 text-xs text-[var(--text-3)]">{{ t("ops.attr_caveat") }}</p>

        <div v-if="outcomes" class="mb-3 flex flex-wrap gap-2">
          <button
            v-for="b in bucketKeys"
            :key="b"
            type="button"
            class="inline-flex items-center gap-1.5 rounded-[var(--radius-pill)] border px-2.5 py-1 text-xs transition-colors"
            :class="bucket === b
              ? 'border-[var(--accent-text)] text-[var(--accent-text)]'
              : 'border-[var(--border)] text-[var(--text-2)] hover:border-[var(--accent-text)]'"
            :aria-pressed="bucket === b"
            :title="t(`ops.bucket_hint.${b}`)"
            @click="pickBucket(b)"
          >
            <span class="size-1.5 rounded-full" :class="bucketClass(b)" />
            {{ bucketLabel(b) }}
            <span class="font-semibold">{{ outcomes.buckets[b] ?? 0 }}</span>
          </button>
        </div>

        <div v-if="outcomes" class="mb-3 flex flex-wrap items-center gap-2 text-xs">
          <select
            v-model="channel"
            class="rounded-[var(--radius-input)] border border-[var(--border)] bg-[var(--surface)] px-2 py-1 text-[var(--text-2)]"
            @change="openId = ''; detail = null; loadOutcomes()"
          >
            <option value="">{{ t("ops.attr_channel_all") }}</option>
            <option v-for="ch in CHANNELS" :key="ch" :value="ch">{{ ch }}</option>
          </select>
          <select
            v-model="kbId"
            class="rounded-[var(--radius-input)] border border-[var(--border)] bg-[var(--surface)] px-2 py-1 text-[var(--text-2)]"
            @change="openId = ''; detail = null; loadOutcomes()"
          >
            <option value="">{{ t("ops.attr_kb_all") }}</option>
            <option v-for="kb in kbs" :key="kb.id" :value="kb.id">{{ kb.name }}</option>
          </select>
          <span v-if="bucket" class="text-[var(--text-3)]">
            {{ t("ops.attr_filtered", { bucket: bucketLabel(bucket) }) }}
          </span>
        </div>

        <p v-if="outcomeError" class="text-xs text-[var(--err)]">⚠️ {{ outcomeError }}</p>
        <div v-else-if="outcomesLoading" class="flex items-center gap-2 py-2 text-xs text-[var(--text-3)]">
          <Loader2 class="size-3.5 animate-spin" />{{ t("common.loading") }}
        </div>
        <!-- 空清单是上线初期的常态（数据只能从现在开始攒），文案按这个口径写 -->
        <p v-else-if="!outcomes?.items.length" class="py-2 text-sm text-[var(--text-3)]">
          {{ t("ops.attr_empty") }}
        </p>
        <ul v-else class="flex flex-col gap-1">
          <li
            v-for="row in outcomes.items"
            :key="row.id"
            class="rounded-[var(--radius-card)] border border-[var(--border)]"
          >
            <button
              type="button"
              class="flex w-full flex-wrap items-center gap-2 p-2 text-left text-sm"
              :aria-expanded="openId === row.id"
              @click="toggle(row)"
            >
              <ChevronDown v-if="openId === row.id" class="size-3.5 shrink-0 text-[var(--text-3)]" />
              <ChevronRight v-else class="size-3.5 shrink-0 text-[var(--text-3)]" />
              <Badge :class="bucketClass(row.bucket)">{{ bucketLabel(row.bucket) }}</Badge>
              <span class="truncate text-[var(--text-2)]">{{ row.question }}</span>
              <span v-if="row.feedback === -1" class="shrink-0 text-xs text-[var(--err)]">
                {{ t("ops.attr_downvoted") }}
              </span>
              <span class="ml-auto shrink-0 text-xs text-[var(--text-3)]">
                {{ row.channel }}
                <template v-if="row.top_score !== null">· {{ row.top_score }}</template>
                · {{ formatTime(row.ts) }}
              </span>
            </button>

            <div v-if="openId === row.id" class="border-t border-[var(--border)] p-3">
              <div v-if="detailLoading" class="flex items-center gap-2 text-xs text-[var(--text-3)]">
                <Loader2 class="size-3.5 animate-spin" />{{ t("common.loading") }}
              </div>
              <template v-else-if="detail">
                <div class="mb-2 text-xs font-medium text-[var(--text-2)]">
                  {{ t("ops.attr_question") }}
                </div>
                <p class="mb-3 whitespace-pre-wrap text-sm text-[var(--text-1)]">{{ detail.question }}</p>
                <div class="mb-2 text-xs font-medium text-[var(--text-2)]">
                  {{ t("ops.attr_answer") }}
                </div>
                <!-- answer=null 是"这个入口没有助手消息"（/v1、飞书、直问），
                     不是"答了但答案是空的"——两者区别照实显示 -->
                <p v-if="detail.answer" class="mb-3 whitespace-pre-wrap text-sm text-[var(--text-2)]">
                  {{ detail.answer }}
                </p>
                <p v-else class="mb-3 text-xs text-[var(--text-3)]">{{ t("ops.attr_no_answer") }}</p>

                <div v-if="detail.citations?.length" class="mb-1 text-xs font-medium text-[var(--text-2)]">
                  {{ t("ops.attr_citations") }}
                </div>
                <ul v-if="detail.citations?.length" class="flex flex-col gap-1">
                  <li
                    v-for="c in detail.citations"
                    :key="c.index"
                    class="truncate text-xs text-[var(--text-3)]"
                  >
                    [{{ c.index }}] {{ c.doc_name }}
                    <template v-if="c.heading_path">· {{ c.heading_path }}</template>
                  </li>
                </ul>

                <dl class="mt-3 grid grid-cols-[auto_1fr] gap-x-4 gap-y-1 text-xs">
                  <dt class="text-[var(--text-3)]">{{ t("ops.attr_counts") }}</dt>
                  <dd class="text-[var(--text-2)]">
                    {{ t("ops.attr_counts_value", {
                      retrieved: detail.retrieved_count,
                      usable: detail.usable_count,
                      score: detail.top_score ?? "—",
                    }) }}
                  </dd>
                  <dt class="text-[var(--text-3)]">{{ t("ops.attr_actor") }}</dt>
                  <dd class="text-[var(--text-2)]">{{ detail.actor ?? t("ops.attr_actor_none") }}</dd>
                  <dt class="text-[var(--text-3)]">{{ t("ops.attr_kb") }}</dt>
                  <dd class="break-all font-mono text-[var(--text-2)]">
                    {{ (detail.kb_ids?.length ? detail.kb_ids : [detail.kb_id]).filter(Boolean).join(", ") || "—" }}
                  </dd>
                </dl>
              </template>
            </div>
          </li>
        </ul>
      </div>
    </template>
    <p v-else class="text-sm text-[var(--text-3)]">{{ t("common.loading") }}</p>
  </article>
</template>
