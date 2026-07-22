<script setup lang="ts">
// 同步连接器管理对话框（对标#3，库详情页入口）：列表（状态/上次同步/
// 增量统计）+ 新建（飞书 wiki，源+间隔+镜像开关）+ 行操作（立即同步/
// 启停/删除）。存在 running 行时 3s 轮询刷新，关闭对话框即停（与文档
// 状态轮询同哲学）。创建/手动同步由后端后台执行，前端只看状态演进。
import { computed, onBeforeUnmount, ref, watch } from "vue";
import { useI18n } from "vue-i18n";
import { toast } from "vue-sonner";
import { Plus, RefreshCw, Trash2 } from "@lucide/vue";
import { Button } from "@/components/ui/button";
import {
  Dialog, DialogContent, DialogHeader, DialogTitle, DialogDescription, DialogFooter,
} from "@/components/ui/dialog";
import { Input } from "@/components/ui/input";
import { Switch } from "@/components/ui/switch";
import {
  Select, SelectContent, SelectItem, SelectTrigger, SelectValue,
} from "@/components/ui/select";
import {
  listConnectors, createConnector, updateConnector, deleteConnector,
  syncConnector, type Connector,
} from "@/lib/api";

const props = defineProps<{ open: boolean; kbId: string | undefined }>();
const emit = defineEmits<{ "update:open": [value: boolean]; changed: [] }>();
const { t } = useI18n();

const rows = ref<Connector[]>([]);
const loading = ref(false);
const error = ref<string | null>(null);

// ---- 新建表单 ----
const showCreate = ref(false);
const source = ref("");
const name = ref("");
const interval = ref("1440");        // Select 值为字符串；"0"=仅手动
const prune = ref(true);
const creating = ref(false);

// ---- 删除确认（含 purge 选项）----
const deleteTarget = ref<Connector | null>(null);
const purgeDocs = ref(false);

// label 存 i18n key，渲染时 t()。
const INTERVALS = [
  { value: "0", label: "connector.int_manual" },
  { value: "60", label: "connector.int_hourly" },
  { value: "360", label: "connector.int_6h" },
  { value: "1440", label: "connector.int_daily" },
  { value: "10080", label: "connector.int_weekly" },
];

function intervalLabel(minutes: number): string {
  const found = INTERVALS.find((i) => i.value === String(minutes));
  return found ? t(found.label) : t("connector.every_n_min", { n: minutes });
}

function fmtTime(iso: string | null): string {
  return iso ? new Date(iso + "Z").toLocaleString() : t("connector.never_synced");
}

function statsText(c: Connector): string {
  const s = c.last_sync_stats;
  if (!s) return "";
  const parts = [];
  if (s.added) parts.push(t("connector.stat_added", { n: s.added }));
  if (s.updated) parts.push(t("connector.stat_updated", { n: s.updated }));
  if (s.pruned) parts.push(t("connector.stat_pruned", { n: s.pruned }));
  if (s.failed) parts.push(t("connector.stat_failed", { n: s.failed }));
  if (!parts.length) parts.push(t("connector.stat_no_change", { n: s.skipped }));
  return parts.join(" · ");
}

const STATUS_META: Record<string, { labelKey: string; cls: string }> = {
  running: { labelKey: "connector.status_running", cls: "bg-[var(--accent)] animate-pulse" },
  done: { labelKey: "connector.status_done", cls: "bg-[var(--ok,#22c55e)]" },
  done_with_errors: { labelKey: "connector.status_partial", cls: "bg-[var(--warn)]" },
  failed: { labelKey: "connector.status_failed", cls: "bg-[var(--err)]" },
};

function statusMeta(c: Connector) {
  return STATUS_META[c.last_sync_status ?? ""] ?? { labelKey: "connector.status_pending", cls: "bg-[var(--border-strong)]" };
}

// running 行存在时 3s 轮询（同步是后台任务，状态经列表演进呈现）
let timer: ReturnType<typeof setInterval> | null = null;
const hasRunning = computed(() => rows.value.some((r) => r.last_sync_status === "running"));

function ensurePolling() {
  if (hasRunning.value && timer === null) {
    timer = setInterval(load, 3000);
  } else if (!hasRunning.value && timer !== null) {
    clearInterval(timer);
    timer = null;
  }
}

function stopPolling() {
  if (timer !== null) {
    clearInterval(timer);
    timer = null;
  }
}

async function load() {
  if (!props.kbId) return;
  try {
    rows.value = await listConnectors(props.kbId);
    ensurePolling();
  } catch (err) {
    stopPolling();
    error.value = err instanceof Error ? err.message : String(err);
  }
}

watch(() => props.open, async (isOpen) => {
  if (!isOpen) {
    stopPolling();
    emit("changed");                  // 关闭时让父组件刷新文档列表
    return;
  }
  error.value = null;
  showCreate.value = false;
  loading.value = true;
  await load();
  loading.value = false;
});

onBeforeUnmount(stopPolling);

async function submitCreate() {
  if (!props.kbId || !source.value.trim()) return;
  creating.value = true;
  error.value = null;
  try {
    await createConnector(props.kbId, {
      type: "feishu",
      source: source.value.trim(),
      name: name.value.trim(),
      interval_minutes: Number(interval.value),
      prune: prune.value,
    });
    toast.success(t("connector.created"));
    source.value = "";
    name.value = "";
    showCreate.value = false;
    await load();
  } catch (err) {
    // 409=凭据未配置：指去已有的凭据配置入口（一次配置处处可用）
    const msg = err instanceof Error ? err.message : String(err);
    error.value = msg.includes("凭据")
      ? t("connector.creds_hint", { msg })
      : msg;
  } finally {
    creating.value = false;
  }
}

async function doSync(c: Connector) {
  error.value = null;
  try {
    await syncConnector(c.id);
    toast.success(t("connector.sync_triggered", { name: c.name || c.config.source }));
    await load();
  } catch (err) {
    error.value = err instanceof Error ? err.message : String(err);
  }
}

async function toggleEnabled(c: Connector, value: boolean) {
  try {
    await updateConnector(c.id, { enabled: value });
    await load();
  } catch (err) {
    error.value = err instanceof Error ? err.message : String(err);
  }
}

async function confirmDelete() {
  if (!deleteTarget.value) return;
  try {
    await deleteConnector(deleteTarget.value.id, purgeDocs.value);
    toast.success(purgeDocs.value ? t("connector.deleted_purge") : t("connector.deleted_kept"));
    await load();
  } catch (err) {
    error.value = err instanceof Error ? err.message : String(err);
  } finally {
    deleteTarget.value = null;
    purgeDocs.value = false;
  }
}
</script>

<template>
  <Dialog :open="open" @update:open="(v) => emit('update:open', v)">
    <DialogContent class="max-w-2xl">
      <DialogHeader>
        <DialogTitle>{{ t("kb.sync_connectors") }}</DialogTitle>
        <DialogDescription>
          {{ t("connector.desc") }}
        </DialogDescription>
      </DialogHeader>

      <div class="flex flex-col gap-3">
        <!-- 列表 -->
        <p v-if="loading" class="text-sm text-[var(--text-3)]">{{ t("common.loading") }}</p>
        <p v-else-if="!rows.length && !showCreate" class="text-sm text-[var(--text-3)]">
          {{ t("connector.empty") }}
        </p>
        <div
          v-for="c in rows"
          :key="c.id"
          class="rounded-[var(--radius-ctl)] border border-[var(--border)] p-3"
        >
          <div class="flex items-center justify-between gap-2">
            <div class="min-w-0">
              <div class="flex items-center gap-2">
                <span class="inline-block size-2 shrink-0 rounded-full" :class="statusMeta(c).cls" />
                <span class="truncate text-sm font-medium">
                  {{ c.name || c.config.source }}
                </span>
                <span class="shrink-0 rounded bg-[var(--surface-2)] px-1.5 py-0.5 text-[10px] text-[var(--text-3)]">
                  {{ t("connector.source_feishu") }} · {{ intervalLabel(c.interval_minutes) }}
                </span>
              </div>
              <p class="mt-1 truncate text-xs text-[var(--text-3)]">
                {{ t(statusMeta(c).labelKey) }} · {{ fmtTime(c.last_sync_at) }}
                · {{ t("connector.doc_count", { count: c.doc_count }) }}
                <template v-if="statsText(c)"> · {{ statsText(c) }}</template>
              </p>
              <p v-if="c.last_sync_error" class="mt-1 text-xs text-[var(--err)]">
                {{ c.last_sync_error }}
              </p>
            </div>
            <div class="flex shrink-0 items-center gap-1.5">
              <Switch
                :model-value="c.enabled"
                :aria-label="t('connector.enable_sync')"
                @update:model-value="(v: boolean) => toggleEnabled(c, v)"
              />
              <Button
                variant="outline" size="sm"
                :disabled="c.last_sync_status === 'running'"
                @click="doSync(c)"
              >
                <RefreshCw class="size-3.5" />
                {{ t("connector.sync_now") }}
              </Button>
              <button
                type="button"
                class="rounded-[var(--radius-ctl)] p-1.5 text-[var(--text-3)] hover:bg-[var(--err-weak)] hover:text-[var(--err)]"
                :aria-label="t('connector.delete')"
                @click="deleteTarget = c"
              >
                <Trash2 class="size-3.5" />
              </button>
            </div>
          </div>
        </div>

        <!-- 新建表单 -->
        <div
          v-if="showCreate"
          class="flex flex-col gap-2 rounded-[var(--radius-ctl)] border border-dashed border-[var(--border-strong)] p-3"
        >
          <Input
            v-model="source"
            :placeholder="t('connector.source_ph')"
            :aria-label="t('feishuimport.source_label')"
          />
          <div class="flex items-center gap-2">
            <Input v-model="name" class="flex-1" :placeholder="t('connector.name_ph')" />
            <Select v-model="interval">
              <SelectTrigger class="w-36"><SelectValue /></SelectTrigger>
              <SelectContent>
                <SelectItem v-for="i in INTERVALS" :key="i.value" :value="i.value">
                  {{ t(i.label) }}
                </SelectItem>
              </SelectContent>
            </Select>
          </div>
          <label class="flex items-center gap-2 text-xs text-[var(--text-2)]">
            <Switch v-model="prune" />
            {{ t("connector.prune_label") }}
          </label>
          <div class="flex justify-end gap-2">
            <Button variant="outline" size="sm" @click="showCreate = false">{{ t("common.cancel") }}</Button>
            <Button size="sm" :disabled="creating || !source.trim()" @click="submitCreate">
              {{ creating ? t("connector.creating") : t("connector.create") }}
            </Button>
          </div>
        </div>
        <Button v-else variant="outline" size="sm" class="self-start" @click="showCreate = true">
          <Plus class="size-3.5" />
          {{ t("connector.new_feishu") }}
        </Button>

        <!-- 错误常驻（生产反馈：toast 一闪而过） -->
        <div
          v-if="error"
          class="rounded-[var(--radius-ctl)] border border-[var(--err)] bg-[var(--err-weak)] p-3 text-xs text-[var(--err)]"
        >
          {{ error }}
        </div>
      </div>

      <DialogFooter>
        <Button variant="outline" @click="emit('update:open', false)">{{ t("common.close") }}</Button>
      </DialogFooter>
    </DialogContent>
  </Dialog>

  <!-- 删除确认（purge 选项） -->
  <Dialog :open="!!deleteTarget" @update:open="(v) => { if (!v) deleteTarget = null; }">
    <DialogContent>
      <DialogHeader>
        <DialogTitle>{{ t("connector.delete") }}</DialogTitle>
        <DialogDescription>
          {{ t("connector.delete_confirm", { name: deleteTarget?.name || deleteTarget?.config.source }) }}
        </DialogDescription>
      </DialogHeader>
      <label class="flex items-center gap-2 text-sm text-[var(--text-2)]">
        <Switch v-model="purgeDocs" />
        {{ t("connector.purge_label", { count: deleteTarget?.doc_count ?? 0 }) }}
      </label>
      <DialogFooter>
        <Button variant="outline" @click="deleteTarget = null">{{ t("common.cancel") }}</Button>
        <Button variant="destructive" @click="confirmDelete">{{ t("common.confirm_delete") }}</Button>
      </DialogFooter>
    </DialogContent>
  </Dialog>
</template>
