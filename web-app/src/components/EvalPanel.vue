<script setup lang="ts">
// 评测回归面板（B）：选库后管理评测集（粘贴 JSON 用例上传）、一键回归
// （hit@k/MRR）、历史 run 对比、逐用例明细（未命中标红，定位知识缺口）。
// 只评检索不评生成——秒级出结果且零 LLM 费用，调参前后各跑一次即可对比。
import { ref, watch } from "vue";
import { Play, Plus, Trash2 } from "@lucide/vue";
import { toast } from "vue-sonner";
import { Button } from "@/components/ui/button";
import {
  Dialog, DialogContent, DialogHeader, DialogTitle, DialogDescription, DialogFooter,
} from "@/components/ui/dialog";
import { Input } from "@/components/ui/input";
import {
  listEvalSets, createEvalSet, deleteEvalSet, runEvalSet, listEvalRuns, getEvalRun,
  type EvalSetItem, type EvalRunResult, type EvalCase,
} from "@/lib/api";

const props = defineProps<{ kbId: string | undefined }>();

const sets = ref<EvalSetItem[]>([]);
const activeSetId = ref<string | null>(null);
const runs = ref<EvalRunResult[]>([]);
const activeRun = ref<EvalRunResult | null>(null);
const running = ref(false);

// 建集对话框：名称 + JSON 用例粘贴区
const createOpen = ref(false);
const newName = ref("");
const newCasesJson = ref("");

const CASES_PLACEHOLDER = `[
  {"question": "住房补贴怎么申领", "expect_doc": "补贴制度.pdf"},
  {"question": "迟到如何处理", "expect_text": "旷工半天"}
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
  activeSetId.value = null;
  runs.value = [];
  activeRun.value = null;
  void refresh();
}, { immediate: true });

async function selectSet(id: string) {
  activeSetId.value = id;
  activeRun.value = null;
  runs.value = await listEvalRuns(id);
}

async function createSet() {
  if (!props.kbId || !newName.value.trim()) return;
  let cases: EvalCase[];
  try {
    cases = JSON.parse(newCasesJson.value);
    if (!Array.isArray(cases) || !cases.length) throw new Error("用例需为非空数组");
  } catch (err) {
    toast.error(`用例 JSON 解析失败：${err instanceof Error ? err.message : String(err)}`);
    return;
  }
  try {
    await createEvalSet(props.kbId, newName.value.trim(), cases);
    toast.success("评测集已创建");
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

async function runNow() {
  if (!activeSetId.value) return;
  running.value = true;
  try {
    activeRun.value = await runEvalSet(activeSetId.value);
    runs.value = await listEvalRuns(activeSetId.value);
    toast.success(`回归完成：hit@k ${pct(activeRun.value.hit_rate)}，MRR ${activeRun.value.mrr.toFixed(3)}`);
  } catch (err) {
    toast.error(err instanceof Error ? err.message : String(err));
  } finally {
    running.value = false;
  }
}

async function openRun(id: string) {
  activeRun.value = await getEvalRun(id);
}

function pct(v: number): string {
  return `${(v * 100).toFixed(1)}%`;
}

// 历史对比：相邻两次 run 的指标差，正=变好（绿）负=变差（红）
function delta(i: number, key: "hit_rate" | "mrr"): number | null {
  if (i >= runs.value.length - 1) return null;
  return runs.value[i][key] - runs.value[i + 1][key];
}
</script>

<template>
  <div class="flex h-full gap-4">
    <!-- 左：评测集列表 -->
    <aside class="w-64 shrink-0">
      <div class="mb-2 flex items-center justify-between">
        <h3 class="text-sm font-medium text-[var(--text-2)]">评测集</h3>
        <Button size="sm" variant="outline" :disabled="!kbId" @click="createOpen = true">
          <Plus class="size-3.5" />
          新建
        </Button>
      </div>
      <p v-if="!sets.length" class="py-6 text-center text-xs text-[var(--text-3)]">
        暂无评测集，新建并粘贴用例 JSON
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
          <div class="text-xs text-[var(--text-3)]">{{ s.case_count }} 条用例</div>
        </div>
        <button
          type="button"
          class="rounded p-1 text-[var(--text-3)] opacity-0 hover:text-[var(--err)] group-hover:opacity-100"
          aria-label="删除评测集"
          @click.stop="removeSet(s.id)"
        >
          <Trash2 class="size-3.5" />
        </button>
      </div>
    </aside>

    <!-- 右：回归执行 + 历史 + 明细 -->
    <section class="min-w-0 flex-1">
      <p v-if="!activeSetId" class="py-12 text-center text-sm text-[var(--text-3)]">
        选择左侧评测集查看回归历史
      </p>
      <template v-else>
        <div class="mb-4 flex items-center gap-3">
          <Button :disabled="running" @click="runNow">
            <Play class="size-3.5" />
            {{ running ? "回归中…" : "一键回归" }}
          </Button>
          <span class="text-xs text-[var(--text-3)]">按该库当前检索策略跑，只评检索不耗 LLM</span>
        </div>

        <h4 class="mb-2 text-sm font-medium text-[var(--text-2)]">历史回归（新→旧）</h4>
        <table class="w-full text-sm">
          <thead>
            <tr class="border-b border-[var(--border)] text-left text-xs text-[var(--text-3)]">
              <th class="py-1.5 font-normal">时间</th>
              <th class="font-normal">hit@k</th>
              <th class="font-normal">MRR</th>
              <th class="font-normal">top_k</th>
              <th class="font-normal">用例</th>
              <th></th>
            </tr>
          </thead>
          <tbody>
            <tr v-for="(r, i) in runs" :key="r.id" class="border-b border-[var(--border)]">
              <td class="py-1.5 text-xs text-[var(--text-3)]">{{ new Date(r.created_at + "Z").toLocaleString() }}</td>
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
              <td>{{ r.top_k }}</td>
              <td>{{ r.total }}</td>
              <td class="text-right">
                <button type="button" class="text-xs text-[var(--accent-text)] hover:underline" @click="openRun(r.id)">
                  明细
                </button>
              </td>
            </tr>
            <tr v-if="!runs.length">
              <td colspan="6" class="py-4 text-center text-xs text-[var(--text-3)]">尚未跑过回归</td>
            </tr>
          </tbody>
        </table>

        <template v-if="activeRun?.details">
          <h4 class="mt-6 mb-2 text-sm font-medium text-[var(--text-2)]">
            逐用例明细（{{ activeRun.details.filter((d) => !d.hit).length }} 条未命中）
          </h4>
          <div
            v-for="(d, i) in activeRun.details"
            :key="i"
            class="mb-1 flex items-center justify-between rounded-[var(--radius-ctl)] border px-3 py-1.5 text-sm"
            :class="d.hit ? 'border-[var(--border)]' : 'border-[var(--err)] bg-[var(--err-weak)]'"
          >
            <span class="min-w-0 truncate">{{ d.question }}</span>
            <span class="ml-3 shrink-0 text-xs" :class="d.hit ? 'text-[var(--ok)]' : 'text-[var(--err)]'">
              {{ d.hit ? `命中 #${d.rank}` : `未命中（top1: ${d.top_doc ?? "无结果"}）` }}
            </span>
          </div>
        </template>
      </template>
    </section>
  </div>

  <!-- 新建评测集对话框 -->
  <Dialog :open="createOpen" @update:open="(v) => (createOpen = v)">
    <DialogContent>
      <DialogHeader>
        <DialogTitle>新建评测集</DialogTitle>
        <DialogDescription>
          每条用例含 question，以及 expect_doc（应命中的文档名）或 expect_text（命中块应包含的文本）之一
        </DialogDescription>
      </DialogHeader>
      <Input v-model="newName" placeholder="评测集名称，如：核心业务问题30条" />
      <textarea
        v-model="newCasesJson"
        rows="10"
        :placeholder="CASES_PLACEHOLDER"
        class="w-full resize-y rounded-[var(--radius-ctl)] border border-[var(--border)] bg-[var(--surface)] px-3 py-2 font-mono text-xs leading-relaxed text-[var(--text)] outline-none focus:border-[var(--accent)]"
        aria-label="用例 JSON"
      />
      <DialogFooter>
        <Button variant="outline" @click="createOpen = false">取消</Button>
        <Button :disabled="!newName.trim() || !newCasesJson.trim()" @click="createSet">创建</Button>
      </DialogFooter>
    </DialogContent>
  </Dialog>
</template>
