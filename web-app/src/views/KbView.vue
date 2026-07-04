<script setup lang="ts">
// 知识库管理页：卡片网格（/kb）+ 单库详情（/kb?kb=<id>，文档表格+上传+配置）。
// 详情态的文档状态轮询：存在 parsing/pending/pending_ocr 时每 3s 刷新一次，
// 全部 ready/failed 后自动停止；组件卸载（离开页面/切换路由）时也停止，
// 避免后台定时器继续对已卸载组件的响应式状态写入。
import { computed, onBeforeUnmount, onMounted, ref, watch } from "vue";
import { useRoute, useRouter } from "vue-router";
import { Plus, RotateCw, Trash2, Settings2, AlertCircle } from "@lucide/vue";
import { toast } from "vue-sonner";
import { Button } from "@/components/ui/button";
import { Input } from "@/components/ui/input";
import { Badge } from "@/components/ui/badge";
import { Switch } from "@/components/ui/switch";
import {
  Dialog, DialogContent, DialogHeader, DialogTitle, DialogDescription, DialogFooter,
} from "@/components/ui/dialog";
import {
  Table, TableHeader, TableBody, TableRow, TableHead, TableCell, TableEmpty,
} from "@/components/ui/table";
import { Tooltip, TooltipContent, TooltipTrigger, TooltipProvider } from "@/components/ui/tooltip";
import UploadZone from "@/components/UploadZone.vue";
import {
  listKbs, createKb, listDocs, uploadDocs, deleteDoc, retryDoc, retryOcr, putKbConfig,
  type Kb, type DocumentItem,
} from "@/lib/api";
import { statusBadge, hasPollingStatus, hasPendingOcr, canRetryDoc } from "@/lib/kb-utils";

const route = useRoute();
const router = useRouter();

const kbs = ref<Kb[]>([]);
const docs = ref<DocumentItem[]>([]);
const docsLoading = ref(false);

const kbId = computed(() => (typeof route.query.kb === "string" ? route.query.kb : undefined));
const currentKb = computed(() => kbs.value.find((k) => k.id === kbId.value) ?? null);

// 卡片网格用：每个 kb 的文档数（并行拉取，失败静默为 0，不阻塞网格渲染）
const docCounts = ref<Record<string, number>>({});

async function loadKbs() {
  kbs.value = await listKbs();
  const counts: Record<string, number> = {};
  await Promise.all(kbs.value.map(async (kb) => {
    try {
      counts[kb.id] = (await listDocs(kb.id)).length;
    } catch {
      counts[kb.id] = 0;
    }
  }));
  docCounts.value = counts;
}

async function loadDocs() {
  if (!kbId.value) return;
  docsLoading.value = true;
  try {
    docs.value = await listDocs(kbId.value);
  } finally {
    docsLoading.value = false;
  }
}

function openKb(id: string) {
  router.push({ path: "/kb", query: { kb: id } });
}

function backToGrid() {
  router.push({ path: "/kb" });
}

// ---- 新建知识库 ----
const createOpen = ref(false);
const newKbName = ref("");
const creating = ref(false);

async function submitCreate() {
  const name = newKbName.value.trim();
  if (!name) return;
  creating.value = true;
  try {
    const kb = await createKb(name);
    createOpen.value = false;
    newKbName.value = "";
    await loadKbs();
    openKb(kb.id);
  } catch (err) {
    toast.error(err instanceof Error ? err.message : String(err));
  } finally {
    creating.value = false;
  }
}

// ---- 上传 ----
async function handleFilesSelected(files: File[]) {
  if (!kbId.value) return;
  // 乐观插入 parsing 行，立即可见；真实状态由下一次轮询/刷新覆盖
  const optimistic: DocumentItem[] = files.map((f) => ({
    id: `optimistic-${f.name}-${Date.now()}-${Math.random()}`,
    filename: f.name,
    status: "parsing",
    error: null,
  }));
  docs.value = [...docs.value, ...optimistic];

  const form = new FormData();
  files.forEach((f) => form.append("files", f));
  try {
    await uploadDocs(kbId.value, form);
    toast.success(`已提交 ${files.length} 个文件`);
  } catch (err) {
    toast.error(err instanceof Error ? err.message : String(err));
  } finally {
    await loadDocs();
  }
}

// ---- 行操作 ----
async function handleRetry(doc: DocumentItem) {
  try {
    await retryDoc(doc.id);
    toast.success(`已重试: ${doc.filename}`);
  } catch (err) {
    toast.error(err instanceof Error ? err.message : String(err));
  } finally {
    await loadDocs();
  }
}

const deleteTarget = ref<DocumentItem | null>(null);

async function confirmDelete() {
  if (!deleteTarget.value || !kbId.value) return;
  try {
    await deleteDoc(kbId.value, deleteTarget.value.id);
    toast.success(`已删除: ${deleteTarget.value.filename}`);
  } catch (err) {
    toast.error(err instanceof Error ? err.message : String(err));
  } finally {
    deleteTarget.value = null;
    await loadDocs();
  }
}

async function handleBatchRetryOcr() {
  if (!kbId.value) return;
  try {
    const r = await retryOcr(kbId.value);
    toast.success(`已批量重试 OCR：${r.retrying.length} 个文档`);
  } catch (err) {
    toast.error(err instanceof Error ? err.message : String(err));
  } finally {
    await loadDocs();
  }
}

// ---- 状态轮询 ----
let pollTimer: ReturnType<typeof setInterval> | null = null;

function stopPolling() {
  if (pollTimer) {
    clearInterval(pollTimer);
    pollTimer = null;
  }
}

function ensurePolling() {
  if (pollTimer) return;
  if (!hasPollingStatus(docs.value)) return;
  pollTimer = setInterval(async () => {
    await loadDocs();
    if (!hasPollingStatus(docs.value)) stopPolling();
  }, 3000);
}

watch(docs, () => ensurePolling(), { deep: true });

watch(kbId, async (id) => {
  stopPolling();
  docs.value = [];
  if (id) {
    await loadDocs();
    ensurePolling();
  }
}, { immediate: true });

onBeforeUnmount(stopPolling);

// ---- KB 配置 Dialog ----
const configOpen = ref(false);
const configChunkSize = ref(500);
const configChunkOverlap = ref(50);
const configEnrichEnabled = ref(false);
const configSaving = ref(false);

function openConfig() {
  const cfg = currentKb.value?.config;
  configChunkSize.value = cfg?.chunk_size ?? 500;
  configChunkOverlap.value = cfg?.chunk_overlap ?? 50;
  configEnrichEnabled.value = cfg?.enrich?.enabled ?? false;
  configOpen.value = true;
}

async function saveConfig() {
  if (!kbId.value) return;
  configSaving.value = true;
  try {
    await putKbConfig(kbId.value, {
      chunk_size: configChunkSize.value,
      chunk_overlap: configChunkOverlap.value,
      enrich: { enabled: configEnrichEnabled.value },
    });
    toast.success("配置已保存");
    configOpen.value = false;
    await loadKbs();
  } catch (err) {
    toast.error(err instanceof Error ? err.message : String(err));
  } finally {
    configSaving.value = false;
  }
}

onMounted(loadKbs);
</script>

<template>
  <div class="p-6">
    <!-- 卡片网格 -->
    <template v-if="!kbId">
      <h1 class="mb-4 text-lg font-semibold">知识库</h1>
      <div class="grid grid-cols-[repeat(auto-fill,minmax(220px,1fr))] gap-4">
        <button
          v-for="kb in kbs"
          :key="kb.id"
          type="button"
          class="rounded-[var(--radius-card)] border border-[var(--border)] bg-[var(--surface)] p-4 text-left transition-colors hover:border-[var(--accent)]"
          @click="openKb(kb.id)"
        >
          <div class="truncate font-medium">{{ kb.name }}</div>
          <div class="mt-1 text-sm text-[var(--text-3)]">{{ docCounts[kb.id] ?? 0 }} 篇文档</div>
        </button>

        <button
          type="button"
          class="flex flex-col items-center justify-center gap-2 rounded-[var(--radius-card)] border-2 border-dashed border-[var(--border-strong)] p-4 text-[var(--text-3)] transition-colors hover:border-[var(--accent)] hover:text-[var(--accent-text)]"
          @click="createOpen = true"
        >
          <Plus class="size-5" />
          <span class="text-sm">新建知识库</span>
        </button>
      </div>
    </template>

    <!-- 单库详情 -->
    <template v-else>
      <div class="mb-4 flex items-center justify-between">
        <div>
          <button type="button" class="text-sm text-[var(--text-3)] hover:text-[var(--text)]" @click="backToGrid">
            ← 返回知识库列表
          </button>
          <h1 class="text-lg font-semibold">{{ currentKb?.name ?? kbId }}</h1>
        </div>
        <div class="flex items-center gap-2">
          <Button
            v-if="hasPendingOcr(docs)"
            variant="outline"
            size="sm"
            @click="handleBatchRetryOcr"
          >
            <RotateCw class="size-3.5" />
            批量重试OCR
          </Button>
          <Button variant="outline" size="sm" @click="openConfig">
            <Settings2 class="size-3.5" />
            知识库配置
          </Button>
        </div>
      </div>

      <UploadZone class="mb-4" @files-selected="handleFilesSelected" />

      <TooltipProvider>
        <Table>
          <TableHeader>
            <TableRow>
              <TableHead>文件名</TableHead>
              <TableHead>状态</TableHead>
              <TableHead class="w-32">操作</TableHead>
            </TableRow>
          </TableHeader>
          <TableBody>
            <TableEmpty v-if="!docsLoading && docs.length === 0" :colspan="3">
              暂无文档，拖拽或选择文件开始上传
            </TableEmpty>
            <TableRow v-for="doc in docs" :key="doc.id">
              <TableCell class="max-w-xs truncate">{{ doc.filename }}</TableCell>
              <TableCell>
                <div class="flex items-center gap-1.5">
                  <Badge :class="statusBadge(doc.status).class">{{ statusBadge(doc.status).label }}</Badge>
                  <Tooltip v-if="doc.status === 'failed' && doc.error">
                    <TooltipTrigger as-child>
                      <AlertCircle class="size-3.5 text-[var(--err)]" />
                    </TooltipTrigger>
                    <TooltipContent>{{ doc.error }}</TooltipContent>
                  </Tooltip>
                </div>
              </TableCell>
              <TableCell>
                <div class="flex items-center gap-1">
                  <Button
                    v-if="canRetryDoc(doc.status)"
                    variant="ghost"
                    size="icon-sm"
                    aria-label="重试"
                    @click="handleRetry(doc)"
                  >
                    <RotateCw class="size-3.5" />
                  </Button>
                  <Button
                    variant="ghost"
                    size="icon-sm"
                    aria-label="删除"
                    @click="deleteTarget = doc"
                  >
                    <Trash2 class="size-3.5" />
                  </Button>
                </div>
              </TableCell>
            </TableRow>
          </TableBody>
        </Table>
      </TooltipProvider>
    </template>
  </div>

  <!-- 新建知识库 Dialog -->
  <Dialog v-model:open="createOpen">
    <DialogContent>
      <DialogHeader>
        <DialogTitle>新建知识库</DialogTitle>
        <DialogDescription>输入知识库名称</DialogDescription>
      </DialogHeader>
      <Input v-model="newKbName" placeholder="知识库名称" @keydown.enter="submitCreate" />
      <DialogFooter>
        <Button variant="outline" @click="createOpen = false">取消</Button>
        <Button :disabled="creating || !newKbName.trim()" @click="submitCreate">创建</Button>
      </DialogFooter>
    </DialogContent>
  </Dialog>

  <!-- 删除确认 Dialog -->
  <Dialog :open="!!deleteTarget" @update:open="(v) => { if (!v) deleteTarget = null; }">
    <DialogContent>
      <DialogHeader>
        <DialogTitle>删除文档</DialogTitle>
        <DialogDescription>
          确认删除「{{ deleteTarget?.filename }}」？此操作不可撤销。
        </DialogDescription>
      </DialogHeader>
      <DialogFooter>
        <Button variant="outline" @click="deleteTarget = null">取消</Button>
        <Button variant="destructive" @click="confirmDelete">确认删除</Button>
      </DialogFooter>
    </DialogContent>
  </Dialog>

  <!-- KB 配置 Dialog -->
  <Dialog v-model:open="configOpen">
    <DialogContent>
      <DialogHeader>
        <DialogTitle>知识库配置</DialogTitle>
        <DialogDescription>分块大小与上下文增强设置，仅影响后续新上传的文档</DialogDescription>
      </DialogHeader>
      <div class="flex flex-col gap-4">
        <label class="flex flex-col gap-1">
          <span class="text-sm text-[var(--text-2)]">分块大小 chunk_size（64-4096）</span>
          <Input v-model.number="configChunkSize" type="number" min="64" max="4096" />
        </label>
        <label class="flex flex-col gap-1">
          <span class="text-sm text-[var(--text-2)]">分块重叠 chunk_overlap（0-512，需小于分块大小）</span>
          <Input v-model.number="configChunkOverlap" type="number" min="0" max="512" />
        </label>
        <label class="flex items-center justify-between">
          <span class="text-sm text-[var(--text-2)]">上下文增强 enrich</span>
          <Switch v-model="configEnrichEnabled" />
        </label>
      </div>
      <DialogFooter>
        <Button variant="outline" @click="configOpen = false">取消</Button>
        <Button :disabled="configSaving" @click="saveConfig">保存</Button>
      </DialogFooter>
    </DialogContent>
  </Dialog>
</template>
