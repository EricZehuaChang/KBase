<script setup lang="ts">
// 评测回归面板（B）：选库后管理评测集（粘贴 JSON 用例上传）、一键回归
// （hit@k/MRR）、历史 run 对比、逐用例明细（未命中标红，定位知识缺口）。
//
// T15 追加"答案级"口径。默认（仅检索）仍是原来那条同步、秒级、零 LLM 调用的
// 路径；答案级要服务端开 evals.answer_judge.enabled（关着时后端返回 422，这里
// 原样报给用户——不静默退回检索口径，否则会以为"答案分没跑出来是结果本来就
// 空"），跑的是 eval_answer 后台任务：建完立刻返回 job id，本组件按 3s 轮询
// 展示步骤进度（与 useJob 同一套终态判定），跑完自动刷历史并展开最新一条。
// 报告：GET /api/eval-runs/{id}/report?format=md|docx，md 页内预览，docx 走
// 浏览器原生下载（与 jobs 产物同一模式）；答案级任务的 artifact.md 就是同一份
// 报告，任务跑完时会随 artifactUrl 预览。
import { computed, onBeforeUnmount, ref, watch } from "vue";
import { useI18n } from "vue-i18n";
import { CheckCircle2, Circle, FileDown, FileText, Loader2, Play, Plus, Trash2, XCircle } from "@lucide/vue";
import { toast } from "vue-sonner";
import { Button } from "@/components/ui/button";
import {
  Dialog, DialogContent, DialogHeader, DialogTitle, DialogDescription, DialogFooter,
} from "@/components/ui/dialog";
import { Input } from "@/components/ui/input";
import {
  artifactUrl, getJob, isTerminalStatus,
  listEvalSets, createEvalSet, deleteEvalSet, runEvalSet, runEvalSetAnswer,
  listEvalRuns, getEvalRun, evalReportUrl, fetchEvalReport,
  type EvalSetItem, type EvalRunResult, type EvalCase, type EvalMode, type Job,
} from "@/lib/api";

const props = defineProps<{ kbId: string | undefined }>();
const { t } = useI18n();

const sets = ref<EvalSetItem[]>([]);
const activeSetId = ref<string | null>(null);
const runs = ref<EvalRunResult[]>([]);
const activeRun = ref<EvalRunResult | null>(null);
const running = ref(false);
const runMode = ref<EvalMode>("retrieval");

// 答案级任务：job id + 轮询到的详情（steps 进度 / 终态 / 产物）
const answerJobId = ref<string | undefined>(undefined);
const answerJob = ref<Job | null>(null);
let pollTimer: ReturnType<typeof setInterval> | null = null;

// 报告预览
const report = ref<string | null>(null);
const reportLoading = ref(false);

// 建集对话框：名称 + JSON 用例粘贴区
const createOpen = ref(false);
const newName = ref("");
const newCasesJson = ref("");

const CASES_PLACEHOLDER = `[
  {"question": "住房补贴怎么申领", "expect_doc": "补贴制度.pdf"},
  {"question": "迟到如何处理", "expect_text": "旷工半天",
   "expected_answer": "迟到三次记旷工半天"}
]`;

async function refresh() {
  if (!props.kbId) return;
  try {
    sets.value = await listEvalSets(props.kbId);
    if (activeSetId.value && !sets.value.some((s) => s.id === activeSetId.value)) {
      activeSetId.value = null;
      runs.value = [];
      activeRun.value = null;
    }
  } catch (err) {
    toast.error(err instanceof Error ? err.message : String(err));
  }
}

watch(() => props.kbId, () => {
  resetAnswerJob();
  activeSetId.value = null;
  runs.value = [];
  activeRun.value = null;
  report.value = null;
  void refresh();
}, { immediate: true });

async function selectSet(id: string) {
  resetAnswerJob();
  activeSetId.value = id;
  activeRun.value = null;
  report.value = null;
  runs.value = await listEvalRuns(id);
}

async function createSet() {
  if (!props.kbId || !newName.value.trim()) return;
  let cases: EvalCase[];
  try {
    cases = JSON.parse(newCasesJson.value);
    if (!Array.isArray(cases) || !cases.length) throw new Error(t("evals.cases_not_array"));
  } catch (err) {
    toast.error(t("evals.cases_json_error", {
      msg: err instanceof Error ? err.message : String(err),
    }));
    return;
  }
  try {
    await createEvalSet(props.kbId, newName.value.trim(), cases);
    toast.success(t("evals.create_ok"));
    createOpen.value = false;
    newName.value = "";
    newCasesJson.value = "";
    await refresh();
  } catch (err) {
    toast.error(err instanceof Error ? err.message : String(err));
  }
}

async function removeSet(id: string) {
  try {
    await deleteEvalSet(id);
    await refresh();
  } catch (err) {
    toast.error(err instanceof Error ? err.message : String(err));
  }
}

// 仅检索：同步返回结果（与改造前一模一样，零 LLM 调用）
async function runNow() {
  if (!activeSetId.value) return;
  running.value = true;
  report.value = null;
  try {
    activeRun.value = await runEvalSet(activeSetId.value);
    runs.value = await listEvalRuns(activeSetId.value);
    toast.success(t("evals.done", {
      hit: pct(activeRun.value.hit_rate), mrr: activeRun.value.mrr.toFixed(3),
    }));
  } catch (err) {
    toast.error(err instanceof Error ? err.message : String(err));
  } finally {
    running.value = false;
  }
}

// 答案级：建 eval_answer 任务后**立刻**返回 job id（判分分钟级，不在请求里等），
// 之后由 pollAnswerJob 每 3s 拉一次进度。
async function runAnswerNow() {
  if (!activeSetId.value) return;
  running.value = true;
  report.value = null;
  try {
    const job = await runEvalSetAnswer(activeSetId.value);
    answerJobId.value = job.id;
    answerJob.value = null;
    toast.success(t("evals.answer_started", { id: job.id.slice(0, 8) }));
    startPolling();
  } catch (err) {
    toast.error(err instanceof Error ? err.message : String(err));
  } finally {
    running.value = false;
  }
}

function resetAnswerJob() {
  stopPolling();
  answerJobId.value = undefined;
  answerJob.value = null;
}

function startPolling() {
  stopPolling();
  void loadAnswerJob();
  pollTimer = setInterval(loadAnswerJob, 3000);
}

function stopPolling() {
  if (pollTimer) {
    clearInterval(pollTimer);
    pollTimer = null;
  }
}

async function loadAnswerJob() {
  const id = answerJobId.value;
  if (!id) return;
  try {
    const job = await getJob(id);
    answerJob.value = job;
    if (isTerminalStatus(job.status)) {
      stopPolling();
      await onAnswerFinished(job);
    }
  } catch (err) {
    stopPolling();
    toast.error(err instanceof Error ? err.message : String(err));
  }
}

// 任务到终态：刷历史、展开最新一条答案级 run（连同它的报告）。
// 任务被标记 done_with_errors 时仍算跑完（个别用例判分失败，快照照样落了）——
// 这时 history 里那条 run 的答案分只看已判成功的用例，报告里另有失败条数。
async function onAnswerFinished(job: Job) {
  if (!activeSetId.value) return;
  if (!["done", "done_with_errors"].includes(job.status)) {
    toast.error(job.error ? `${t("evals.run_failed")}：${job.error}` : t("evals.run_failed"));
  }
  runs.value = await listEvalRuns(activeSetId.value);
  const latest = runs.value.find((r) => r.mode === "answer");
  if (latest) {
    activeRun.value = await getEvalRun(latest.id);
    const score = latest.answer_score;
    toast.success(t("evals.done_answer", {
      hit: pct(latest.hit_rate), mrr: latest.mrr.toFixed(3),
      score: score === null || score === undefined ? "—" : score.toFixed(3),
    }));
  } else {
    toast.warning(t("evals.run_failed"));
  }
}

async function openRun(id: string) {
  activeRun.value = await getEvalRun(id);
  report.value = null;
}

async function loadReport(id: string) {
  reportLoading.value = true;
  try {
    report.value = await fetchEvalReport(id);
  } catch (err) {
    report.value = null;
    toast.error(err instanceof Error ? err.message : String(err));
  } finally {
    reportLoading.value = false;
  }
}

function pct(v: number): string {
  return `${(v * 100).toFixed(1)}%`;
}

function scoreText(v: number | null | undefined): string {
  return v === null || v === undefined ? t("evals.no_answer_score") : v.toFixed(3);
}

function scoreTitle(v: number | null | undefined): string {
  return v === null || v === undefined ? t("evals.answer_score_hint") : "";
}

// 历史对比：相邻两次 run 的指标差，正=变好（绿）负=变差（红）
function delta(i: number, key: "hit_rate" | "mrr"): number | null {
  if (i >= runs.value.length - 1) return null;
  return runs.value[i][key] - runs.value[i + 1][key];
}

const missedCount = computed(() =>
  (activeRun.value?.details ?? []).filter((d) => d.hit === false).length);
const answerFinished = computed(() =>
  !!answerJob.value && isTerminalStatus(answerJob.value.status));

function stepIcon(status: string) {
  if (status === "done") return CheckCircle2;
  if (status === "failed") return XCircle;
  if (status === "running") return Loader2;
  return Circle;
}

function stepIconClass(status: string) {
  if (status === "done") return "text-[var(--ok)]";
  if (status === "failed") return "text-[var(--err)]";
  if (status === "running") return "text-[var(--accent-text)] animate-spin";
  return "text-[var(--text-3)]";
}

onBeforeUnmount(stopPolling);
</script>

<template>
  <div class="flex h-full gap-4">
    <!-- 左：评测集列表 -->
    <aside class="w-64 shrink-0">
      <div class="mb-2 flex items-center justify-between">
        <h3 class="text-sm font-medium text-[var(--text-2)]">{{ t("evals.sets_title") }}</h3>
        <Button size="sm" variant="outline" :disabled="!kbId" @click="createOpen = true">
          <Plus class="size-3.5" />
          {{ t("evals.new") }}
        </Button>
      </div>
      <p v-if="!sets.length" class="py-6 text-center text-xs text-[var(--text-3)]">
        {{ t("evals.sets_empty") }}
      </p>
      <div
        v-for="s in sets"
        :key="s.id"
        class="group mb-1 flex cursor-pointer items-center justify-between rounded-[var(--radius-ctl)] border px-3 py-2 text-sm"
        :class="s.id === activeSetId
          ? 'border-[var(--accent)] bg-[var(--accent-weak)]'
          : 'border-[var(--border)] hover:bg-[var(--surface-2)]'"
        @click="selectSet(s.id)"
      >
        <div class="min-w-0">
          <div class="truncate">{{ s.name }}</div>
          <div class="text-xs text-[var(--text-3)]">{{ t("evals.cases_count", { count: s.case_count }) }}</div>
        </div>
        <button
          type="button"
          class="rounded p-1 text-[var(--text-3)] opacity-0 hover:text-[var(--err)] group-hover:opacity-100"
          :aria-label="t('evals.delete_set')"
          @click.stop="removeSet(s.id)"
        >
          <Trash2 class="size-3.5" />
        </button>
      </div>
    </aside>

    <!-- 右：回归执行 + 历史 + 明细 -->
    <section class="min-w-0 flex-1">
      <p v-if="!activeSetId" class="py-12 text-center text-sm text-[var(--text-3)]">
        {{ t("evals.select_set") }}
      </p>
      <template v-else>
        <!-- 口径切换：仅检索（默认）/ 答案级（需服务端开开关） -->
        <div class="mb-3 flex flex-wrap items-center gap-2">
          <span class="text-xs text-[var(--text-3)]">{{ t("evals.mode_label") }}</span>
          <div class="inline-flex overflow-hidden rounded-[var(--radius-ctl)] border border-[var(--border)]">
            <button
              v-for="m in (['retrieval', 'answer'] as EvalMode[])"
              :key="m"
              type="button"
              class="px-2.5 py-1 text-xs"
              :class="runMode === m
                ? 'bg-[var(--accent-weak)] text-[var(--accent-text)]'
                : 'text-[var(--text-2)] hover:bg-[var(--surface-2)]'"
              @click="runMode = m"
            >
              {{ m === "retrieval" ? t("evals.mode_retrieval") : t("evals.mode_answer") }}
            </button>
          </div>
        </div>

        <div class="mb-4 flex flex-wrap items-center gap-3">
          <Button v-if="runMode === 'retrieval'" :disabled="running" @click="runNow">
            <Play class="size-3.5" />
            {{ running ? t("evals.running") : t("evals.run_retrieval") }}
          </Button>
          <Button v-else :disabled="running || (!!answerJobId && !answerFinished)" @click="runAnswerNow">
            <Play class="size-3.5" />
            {{ running ? t("evals.running") : t("evals.run_answer") }}
          </Button>
          <span class="text-xs text-[var(--text-3)]">
            {{ runMode === "retrieval" ? t("evals.run_retrieval_hint") : t("evals.run_answer_hint") }}
          </span>
        </div>

        <!-- 答案级任务的实时进度（步骤清单 + 跑完后的报告预览/下载） -->
        <div v-if="answerJobId" class="mb-5 rounded-[var(--radius-card)] border border-[var(--border)] bg-[var(--surface)] p-4">
          <div class="mb-2 flex flex-wrap items-center justify-between gap-2">
            <h4 class="text-sm font-medium text-[var(--text-2)]">{{ t("evals.job_steps") }}</h4>
            <span v-if="!answerFinished" class="text-xs text-[var(--text-3)]">{{ t("evals.answer_running_hint") }}</span>
          </div>

          <ul v-if="answerJob?.progress?.steps.length" class="flex flex-col gap-1.5">
            <li v-for="step in answerJob.progress.steps" :key="step.name" class="flex items-center gap-2 text-sm">
              <component :is="stepIcon(step.status)" class="size-4 shrink-0" :class="stepIconClass(step.status)" />
              <span :class="step.status === 'failed' ? 'text-[var(--err)]' : 'text-[var(--text)]'">{{ step.name }}</span>
              <span v-if="step.detail" class="truncate text-xs text-[var(--text-3)]">（{{ step.detail }}）</span>
            </li>
          </ul>
          <p v-else class="text-xs text-[var(--text-3)]">{{ t("evals.answer_running") }}</p>

          <div v-if="answerFinished" class="mt-3 flex flex-col gap-2">
            <p v-if="answerJob?.status === 'done'" class="text-sm text-[var(--ok)]">{{ t("evals.done") }}</p>
            <p v-else-if="answerJob?.status === 'done_with_errors'" class="text-sm text-[var(--warn)]">
              {{ t("evals.done_answer", { hit: "—", mrr: "—", score: "—" }) }}
            </p>
            <p v-else class="text-sm text-[var(--err)]">
              {{ t("evals.run_failed") }}{{ answerJob?.error ? `：${answerJob.error}` : "" }}
            </p>
            <div v-if="answerJob?.artifact_path" class="flex gap-2">
              <Button as-child size="sm" variant="outline">
                <a :href="artifactUrl(answerJobId!, 'md')" download>{{ t("evals.report_md") }}</a>
              </Button>
              <Button as-child size="sm" variant="outline">
                <a :href="artifactUrl(answerJobId!, 'docx')" download>
                  <FileDown class="size-3.5" />
                  {{ t("evals.report_docx") }}
                </a>
              </Button>
            </div>
          </div>
        </div>

        <h4 class="mb-2 text-sm font-medium text-[var(--text-2)]">{{ t("evals.history") }}</h4>
        <table class="w-full text-sm">
          <thead>
            <tr class="border-b border-[var(--border)] text-left text-xs text-[var(--text-3)]">
              <th class="py-1.5 font-normal">{{ t("evals.col_time") }}</th>
              <th class="font-normal">{{ t("evals.col_mode") }}</th>
              <th class="font-normal">hit{'@'}k</th>
              <th class="font-normal">MRR</th>
              <th class="font-normal">{{ t("evals.answer_score") }}</th>
              <th class="font-normal">{{ t("evals.col_topk") }}</th>
              <th class="font-normal">{{ t("evals.col_cases") }}</th>
              <th></th>
            </tr>
          </thead>
          <tbody>
            <tr v-for="(r, i) in runs" :key="r.id" class="border-b border-[var(--border)]">
              <td class="py-1.5 text-xs text-[var(--text-3)]">{{ new Date(r.created_at + "Z").toLocaleString() }}</td>
              <td class="text-xs">
                <span :class="r.mode === 'answer' ? 'text-[var(--accent-text)]' : 'text-[var(--text-3)]'">
                  {{ r.mode === "answer" ? t("evals.mode_answer") : t("evals.mode_retrieval") }}
                </span>
              </td>
              <td>
                {{ pct(r.hit_rate) }}
                <span v-if="delta(i, 'hit_rate') !== null && delta(i, 'hit_rate') !== 0"
                      class="ml-1 text-xs"
                      :class="delta(i, 'hit_rate')! > 0 ? 'text-[var(--ok)]' : 'text-[var(--err)]'">
                  {{ delta(i, "hit_rate")! > 0 ? "▲" : "▼" }}{{ pct(Math.abs(delta(i, "hit_rate")!)) }}
                </span>
              </td>
              <td>
                {{ r.mrr.toFixed(3) }}
                <span v-if="delta(i, 'mrr') !== null && delta(i, 'mrr') !== 0"
                      class="ml-1 text-xs"
                      :class="delta(i, 'mrr')! > 0 ? 'text-[var(--ok)]' : 'text-[var(--err)]'">
                  {{ delta(i, "mrr")! > 0 ? "▲" : "▼" }}{{ Math.abs(delta(i, "mrr")!).toFixed(3) }}
                </span>
              </td>
              <td :title="scoreTitle(r.answer_score)"
                  :class="r.answer_score === null || r.answer_score === undefined ? 'text-[var(--text-3)]' : ''">
                {{ scoreText(r.answer_score) }}
              </td>
              <td>{{ r.top_k }}</td>
              <td>{{ r.total }}</td>
              <td class="text-right">
                <button type="button" class="text-xs text-[var(--accent-text)] hover:underline" @click="openRun(r.id)">
                  {{ t("evals.detail") }}
                </button>
              </td>
            </tr>
            <tr v-if="!runs.length">
              <td colspan="8" class="py-4 text-center text-xs text-[var(--text-3)]">{{ t("evals.no_runs") }}</td>
            </tr>
          </tbody>
        </table>

        <template v-if="activeRun?.details">
          <div class="mt-6 mb-2 flex flex-wrap items-center justify-between gap-2">
            <h4 class="text-sm font-medium text-[var(--text-2)]">
              {{ activeRun.mode === "answer"
                ? t("evals.details_title")
                : t("evals.details_title_miss", { count: missedCount }) }}
            </h4>
            <div class="flex flex-wrap items-center gap-2">
              <span v-if="activeRun.mode === 'answer'" class="text-xs text-[var(--text-3)]">
                {{ t("evals.answer_score") }}：{{ scoreText(activeRun.answer_score) }}
                · {{ t("evals.judge_provider") }}：{{ activeRun.judge_provider ?? "—" }}
                <template v-if="activeRun.judge_failed_count">
                  · {{ t("evals.judge_failed", { count: activeRun.judge_failed_count }) }}
                </template>
              </span>
              <Button size="sm" variant="outline" @click="loadReport(activeRun.id)">
                <FileText class="size-3.5" />
                {{ t("evals.report") }}
              </Button>
              <Button as-child size="sm" variant="outline">
                <a :href="evalReportUrl(activeRun.id, 'md')" download>{{ t("evals.report_md") }}</a>
              </Button>
              <Button as-child size="sm" variant="outline">
                <a :href="evalReportUrl(activeRun.id, 'docx')" download>
                  <FileDown class="size-3.5" />
                  {{ t("evals.report_docx") }}
                </a>
              </Button>
            </div>
          </div>

          <div v-if="reportLoading" class="mb-3 text-xs text-[var(--text-3)]">{{ t("evals.report_loading") }}</div>
          <pre
            v-else-if="report"
            class="mb-3 max-h-[40vh] overflow-y-auto whitespace-pre-wrap rounded-[var(--radius-card)] border border-[var(--border)] bg-[var(--surface)] p-3 text-xs leading-relaxed"
          >{{ report }}</pre>

          <div
            v-for="(d, i) in activeRun.details"
            :key="i"
            class="mb-1 rounded-[var(--radius-ctl)] border px-3 py-1.5 text-sm"
            :class="d.retrieval_judged === false
              ? 'border-[var(--border)]'
              : (d.hit ? 'border-[var(--border)]' : 'border-[var(--err)] bg-[var(--err-weak)]')"
          >
            <div class="flex items-center justify-between">
              <span class="min-w-0 truncate">{{ d.question }}</span>
              <span class="ml-3 shrink-0 text-xs"
                    :class="d.retrieval_judged === false
                      ? 'text-[var(--text-3)]'
                      : (d.hit ? 'text-[var(--ok)]' : 'text-[var(--err)]')">
                <template v-if="d.retrieval_judged === false">{{ t("evals.detail_not_judged") }}</template>
                <template v-else-if="d.hit">{{ t("evals.detail_hit", { rank: d.rank }) }}</template>
                <template v-else>{{ t("evals.detail_miss", { doc: d.top_doc ?? "—" }) }}</template>
              </span>
            </div>
            <div v-if="d.answer_score !== undefined" class="mt-1 flex items-start justify-between gap-3 text-xs">
              <span class="min-w-0" :class="d.judge_error === 'judge_failed' ? 'text-[var(--warn)]' : 'text-[var(--text-3)]'">
                {{ t("evals.detail_answer_reason") }}：{{ d.answer_reason ?? t("evals.detail_answer_na") }}
              </span>
              <span class="shrink-0" :class="d.answer_score === null ? 'text-[var(--warn)]' : 'text-[var(--text-2)]'">
                {{ t("evals.answer_score") }} {{ scoreText(d.answer_score) }}
              </span>
            </div>
          </div>
        </template>
      </template>
    </section>
  </div>

  <!-- 新建评测集对话框 -->
  <Dialog :open="createOpen" @update:open="(v) => (createOpen = v)">
    <DialogContent>
      <DialogHeader>
        <DialogTitle>{{ t("evals.create_title") }}</DialogTitle>
        <DialogDescription>{{ t("evals.create_desc") }}</DialogDescription>
      </DialogHeader>
      <Input v-model="newName" :placeholder="t('evals.name_placeholder')" />
      <textarea
        v-model="newCasesJson"
        rows="10"
        :placeholder="CASES_PLACEHOLDER"
        class="w-full resize-y rounded-[var(--radius-ctl)] border border-[var(--border)] bg-[var(--surface)] px-3 py-2 font-mono text-xs leading-relaxed text-[var(--text)] outline-none focus:border-[var(--accent)]"
        :aria-label="t('evals.cases_label')"
      />
      <DialogFooter>
        <Button variant="outline" @click="createOpen = false">{{ t("evals.cancel") }}</Button>
        <Button :disabled="!newName.trim() || !newCasesJson.trim()" @click="createSet">{{ t("evals.create") }}</Button>
      </DialogFooter>
    </DialogContent>
  </Dialog>
</template>
