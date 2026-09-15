<script setup lang="ts">
// 导入记录（T18）：知识库详情页的只读 tab —— 每一轮 CLI 灌库（
// python -m kbase.bulk_import）在 import_batches 里落一行，这里读它。
//
// 只读是硬约定，不是"这期先不做写"：网页上传适合日常增量，万级文件的首次
// 灌库必须能断点续传，而 BackgroundTasks 进程内任务重启即丢——触发导入的入口
// 只有命令行（理由写在 kbase/bulk_import.py 的文件头与
// kbase/api/routes/import_batches.py 里）。所以本组件**没有**任何"开始导入"
// 按钮，只有清单/明细/导出。
import { computed, onMounted, ref, watch } from "vue";
import { useI18n } from "vue-i18n";
import { AlertCircle, ChevronDown, ChevronRight, FileDown, Loader2, RefreshCw } from "@lucide/vue";
import { toast } from "vue-sonner";
import { Badge } from "@/components/ui/badge";
import { Button } from "@/components/ui/button";
import { Switch } from "@/components/ui/switch";
import {
  currentRole, getImportEntries, importBatchExportUrl, listImportBatches,
  type ImportBatch, type ImportEntry, type ImportBatchStatus,
} from "@/lib/api";
import { canManageContent } from "@/lib/auth-utils";

const props = defineProps<{ kbId: string }>();

const { t } = useI18n();

// 导出走 viewer 门槛（后端 require_viewer），但仍按"能管内容"控制可见性：
// 与页面其余运维动作同一把尺子，避免 viewer 看到一个点了会 403 的按钮。
const canExport = computed(() => canManageContent(currentRole.value ?? ""));

// ---- 状态色：直接用设计令牌（与 DocumentTable 的 statusBadge 同一手法）----
const STATUS_CLASS: Record<ImportBatchStatus, string> = {
  running: "bg-[var(--warn-weak)] text-[var(--warn)]",
  done: "bg-[var(--ok-weak)] text-[var(--ok)]",
  done_with_errors: "bg-[var(--warn-weak)] text-[var(--warn)]",
  failed: "bg-[var(--err-weak)] text-[var(--err)]",
  interrupted: "bg-[var(--err-weak)] text-[var(--err)]",
};

function statusClass(status: ImportBatchStatus): string {
  return STATUS_CLASS[status] ?? "bg-[var(--surface-2)] text-[var(--text-2)]";
}

function statusLabel(status: ImportBatchStatus): string {
  // t() 查不到时回落原始码（后端将来新增状态不会渲染成空白）
  const key = `import_batches.status.${status}`;
  const translated = t(key);
  return translated !== key ? translated : status;
}

/** UTC naive ISO（后端写的是 utcnow）→ 本地可读时间。解析失败原样显示。 */
function formatTime(iso: string | null): string {
  if (!iso) return "";
  const d = new Date(iso.endsWith("Z") ? iso : `${iso}Z`);
  if (Number.isNaN(d.getTime())) return iso;
  return d.toLocaleString();
}

function formatElapsed(sec: number | null | undefined): string {
  if (sec === null || sec === undefined) return "";
  if (sec < 60) return t("import_batches.elapsed_s", { n: sec.toFixed(1) });
  return t("import_batches.elapsed_min", { n: (sec / 60).toFixed(1) });
}

// ---- 清单 ----
const batches = ref<ImportBatch[]>([]);
const loading = ref(false);
const failed = ref(false);

async function load(): Promise<void> {
  loading.value = true;
  failed.value = false;
  try {
    const r = await listImportBatches(props.kbId);
    batches.value = r.items;
  } catch (err) {
    failed.value = true;
    toast.error(err instanceof Error ? err.message : String(err));
  } finally {
    loading.value = false;
  }
}

// ---- 明细（展开某一行时才拉，避免一次拉几十个批次的清单文件）----
const openId = ref<string | null>(null);
const entries = ref<ImportEntry[]>([]);
const entriesTotal = ref(0);
const entriesFailures = ref(0);
const entriesLoading = ref(false);
const failuresOnly = ref(false);

async function loadEntries(): Promise<void> {
  if (!openId.value) return;
  entriesLoading.value = true;
  try {
    const r = await getImportEntries(openId.value, { failuresOnly: failuresOnly.value });
    entries.value = r.items;
    entriesTotal.value = r.total;
    entriesFailures.value = r.failures;
  } catch (err) {
    entries.value = [];
    toast.error(err instanceof Error ? err.message : String(err));
  } finally {
    entriesLoading.value = false;
  }
}

function toggle(batch: ImportBatch): void {
  if (openId.value === batch.id) {
    openId.value = null;
    return;
  }
  openId.value = batch.id;
  failuresOnly.value = false;
  entries.value = [];
  void loadEntries();
}

watch(failuresOnly, () => { void loadEntries(); });
// 换库时重新拉：组件被 KbView 以 :kb-id 复用，不重置会显示上一个库的记录
watch(() => props.kbId, () => {
  openId.value = null;
  entries.value = [];
  void load();
});

onMounted(load);
</script>

<template>
  <div>
    <div class="mb-3 flex items-center justify-between gap-3">
      <div>
        <h2 class="text-sm font-medium text-[var(--text-2)]">{{ t("import_batches.title") }}</h2>
        <p class="mt-1 text-xs text-[var(--text-3)]">{{ t("import_batches.hint") }}</p>
      </div>
      <Button variant="outline" size="sm" :disabled="loading" @click="load">
        <RefreshCw class="size-3.5" :class="loading ? 'animate-spin' : ''" />
        {{ t("import_batches.refresh") }}
      </Button>
    </div>

    <div v-if="loading && !batches.length" class="flex items-center gap-2 py-8 text-sm text-[var(--text-3)]">
      <Loader2 class="size-4 animate-spin" />{{ t("common.loading") }}
    </div>

    <div v-else-if="failed && !batches.length" class="flex items-center gap-2 py-8 text-sm text-[var(--err)]">
      <AlertCircle class="size-4" />{{ t("import_batches.load_failed") }}
    </div>

    <p v-else-if="!batches.length" class="py-8 text-sm text-[var(--text-3)]">
      {{ t("import_batches.empty") }}
    </p>

    <ul v-else class="flex flex-col gap-2">
      <li
        v-for="batch in batches"
        :key="batch.id"
        class="rounded-[var(--radius-card)] border border-[var(--border)] bg-[var(--surface)]"
      >
        <button
          type="button"
          class="flex w-full flex-wrap items-center gap-3 p-3 text-left"
          :aria-expanded="openId === batch.id"
          @click="toggle(batch)"
        >
          <ChevronDown v-if="openId === batch.id" class="size-4 shrink-0 text-[var(--text-3)]" />
          <ChevronRight v-else class="size-4 shrink-0 text-[var(--text-3)]" />
          <span class="font-mono text-xs text-[var(--text-2)]">{{ batch.id.slice(0, 8) }}</span>
          <Badge :class="statusClass(batch.status)">{{ statusLabel(batch.status) }}</Badge>
          <span class="text-sm text-[var(--text-2)]">
            {{ t("import_batches.counts", {
              done: batch.summary.done ?? 0,
              failed: batch.summary.failed ?? 0,
              total: batch.summary.total ?? 0,
            }) }}
          </span>
          <span v-if="formatElapsed(batch.summary.elapsed_s)" class="text-xs text-[var(--text-3)]">
            {{ formatElapsed(batch.summary.elapsed_s) }}
          </span>
          <span class="ml-auto text-xs text-[var(--text-3)]">
            {{ formatTime(batch.started_at) }}
            <template v-if="batch.started_by">· {{ batch.started_by }}</template>
          </span>
        </button>

        <div v-if="openId === batch.id" class="border-t border-[var(--border)] p-3">
          <dl class="mb-3 grid grid-cols-[auto_1fr] gap-x-4 gap-y-1 text-xs">
            <dt class="text-[var(--text-3)]">{{ t("import_batches.manifest") }}</dt>
            <dd class="break-all font-mono text-[var(--text-2)]">{{ batch.manifest_path }}</dd>
            <dt class="text-[var(--text-3)]">{{ t("import_batches.started_at") }}</dt>
            <dd class="text-[var(--text-2)]">{{ formatTime(batch.started_at) }}</dd>
            <dt class="text-[var(--text-3)]">{{ t("import_batches.finished_at") }}</dt>
            <dd class="text-[var(--text-2)]">
              {{ formatTime(batch.finished_at) || t("import_batches.still_running") }}
            </dd>
            <template v-if="batch.summary.dir">
              <dt class="text-[var(--text-3)]">{{ t("import_batches.source_dir") }}</dt>
              <dd class="break-all font-mono text-[var(--text-2)]">{{ batch.summary.dir }}</dd>
            </template>
            <template v-if="batch.summary.error">
              <dt class="text-[var(--text-3)]">{{ t("import_batches.error") }}</dt>
              <dd class="break-all text-[var(--err)]">{{ batch.summary.error }}</dd>
            </template>
          </dl>

          <div class="mb-3 flex items-center gap-4">
            <label class="flex items-center gap-2 text-xs text-[var(--text-2)]">
              <Switch v-model="failuresOnly" />
              {{ t("import_batches.failures_only") }}
              <span v-if="entriesFailures" class="text-[var(--err)]">
                {{ t("import_batches.failed_count", { n: entriesFailures }) }}
              </span>
            </label>
            <a
              v-if="canExport"
              class="ml-auto inline-flex items-center gap-1.5 text-xs text-[var(--accent-text)] hover:underline"
              :href="importBatchExportUrl(batch.id, failuresOnly)"
              download
            >
              <FileDown class="size-3.5" />{{ t("import_batches.export_csv") }}
            </a>
          </div>

          <div v-if="entriesLoading" class="flex items-center gap-2 py-4 text-xs text-[var(--text-3)]">
            <Loader2 class="size-3.5 animate-spin" />{{ t("common.loading") }}
          </div>
          <p v-else-if="!entries.length" class="py-4 text-xs text-[var(--text-3)]">
            {{ failuresOnly ? t("import_batches.no_failures") : t("import_batches.no_entries") }}
          </p>
          <template v-else>
            <table class="w-full text-left text-xs">
              <thead class="text-[var(--text-3)]">
                <tr>
                  <th class="py-1 pr-2 font-normal">{{ t("import_batches.col_path") }}</th>
                  <th class="py-1 pr-2 font-normal">{{ t("import_batches.col_status") }}</th>
                  <th class="py-1 pr-2 font-normal">{{ t("import_batches.col_doc") }}</th>
                  <th class="py-1 font-normal">{{ t("import_batches.col_error") }}</th>
                </tr>
              </thead>
              <tbody>
                <tr v-for="entry in entries" :key="entry.path" class="border-t border-[var(--border)]">
                  <td class="break-all py-1 pr-2 font-mono text-[var(--text-2)]">{{ entry.path }}</td>
                  <td class="py-1 pr-2">
                    <Badge :class="entry.status === 'failed'
                      ? 'bg-[var(--err-weak)] text-[var(--err)]'
                      : 'bg-[var(--ok-weak)] text-[var(--ok)]'">
                      {{ entry.status === 'failed' ? t("import_batches.entry_failed")
                        : t("import_batches.entry_done") }}
                    </Badge>
                  </td>
                  <td class="py-1 pr-2 text-[var(--text-3)]">
                    {{ entry.doc_id ? entry.doc_id.slice(0, 8) : t("import_batches.no_doc") }}
                  </td>
                  <td class="break-all py-1 text-[var(--err)]">{{ entry.error || "" }}</td>
                </tr>
              </tbody>
            </table>
            <p v-if="entriesTotal > entries.length" class="mt-2 text-xs text-[var(--text-3)]">
              {{ t("import_batches.truncated", { shown: entries.length, total: entriesTotal }) }}
            </p>
          </template>
        </div>
      </li>
    </ul>
  </div>
</template>
