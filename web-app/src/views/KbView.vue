<script setup lang="ts">
// 知识库管理页：卡片网格（/kb）+ 单库详情（/kb?kb=<id>，文档表格+上传+配置）。
// 详情态的文档状态轮询：存在 parsing/pending/pending_ocr 时每 3s 刷新一次，
// 全部 ready/failed 后自动停止；组件卸载（离开页面/切换路由）时也停止，
// 避免后台定时器继续对已卸载组件的响应式状态写入。
import { computed, onBeforeUnmount, onMounted, ref } from "vue";
import { useRoute, useRouter } from "vue-router";
import { Plus, RotateCw, Settings2, Trash2 } from "@lucide/vue";
import { toast } from "vue-sonner";
import { Button } from "@/components/ui/button";
import { Input } from "@/components/ui/input";
import {
  Dialog, DialogContent, DialogHeader, DialogTitle, DialogDescription, DialogFooter,
} from "@/components/ui/dialog";
import UploadZone from "@/components/UploadZone.vue";
import DocumentTable from "@/components/DocumentTable.vue";
import KbConfigDialog from "@/components/KbConfigDialog.vue";
import {
  listKbs, createKb, deleteKb, listDocs, uploadDocs, deleteDoc, retryDoc, retryOcr,
  currentRole,
  type Kb, type DocumentItem,
} from "@/lib/api";
import { hasPendingOcr } from "@/lib/kb-utils";
import { canManageContent } from "@/lib/auth-utils";
import { useKbDocs } from "@/composables/useKbDocs";

// viewer 隐藏上传/删除/新建库按钮（后端已用 require_editor 强制校验，这里
// 只是防呆，不替代后端）。
const canManage = computed(() => canManageContent(currentRole.value ?? ""));

const route = useRoute();
const router = useRouter();

const kbs = ref<Kb[]>([]);

const kbId = computed(() => (typeof route.query.kb === "string" ? route.query.kb : undefined));
const currentKb = computed(() => kbs.value.find((k) => k.id === kbId.value) ?? null);

const { docs, loading: docsLoading, loadDocs, stopPolling } = useKbDocs(kbId);

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

// ---- 删除知识库 ----
const kbDeleteTarget = ref<Kb | null>(null);
const deletingKb = ref(false);

async function confirmDeleteKb() {
  if (!kbDeleteTarget.value) return;
  deletingKb.value = true;
  try {
    await deleteKb(kbDeleteTarget.value.id);
    toast.success(`已删除知识库: ${kbDeleteTarget.value.name}`);
    if (kbId.value === kbDeleteTarget.value.id) backToGrid();
  } catch (err) {
    toast.error(err instanceof Error ? err.message : String(err));
  } finally {
    deletingKb.value = false;
    kbDeleteTarget.value = null;
    await loadKbs();
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

onBeforeUnmount(stopPolling);

const configOpen = ref(false);

onMounted(loadKbs);
</script>

<template>
  <div class="p-6">
    <!-- 卡片网格 -->
    <template v-if="!kbId">
      <h1 class="mb-4 text-lg font-semibold">知识库</h1>
      <div class="grid grid-cols-[repeat(auto-fill,minmax(220px,1fr))] gap-4">
        <div
          v-for="kb in kbs"
          :key="kb.id"
          role="button"
          tabindex="0"
          class="group relative rounded-[var(--radius-card)] border border-[var(--border)] bg-[var(--surface)] p-4 text-left transition-colors hover:border-[var(--accent)]"
          @click="openKb(kb.id)"
          @keydown.enter="openKb(kb.id)"
          @keydown.space.prevent="openKb(kb.id)"
        >
          <button
            v-if="canManage"
            type="button"
            class="absolute right-2 top-2 rounded-[var(--radius-ctl)] p-1 text-[var(--text-3)] opacity-0 transition-opacity hover:bg-[var(--err-weak)] hover:text-[var(--err)] focus-visible:opacity-100 group-hover:opacity-100"
            aria-label="删除知识库"
            @click.stop="kbDeleteTarget = kb"
          >
            <Trash2 class="size-3.5" />
          </button>
          <div class="truncate pr-6 font-medium">{{ kb.name }}</div>
          <div class="mt-1 text-sm text-[var(--text-3)]">{{ docCounts[kb.id] ?? 0 }} 篇文档</div>
        </div>

        <button
          v-if="canManage"
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
            v-if="canManage && hasPendingOcr(docs)"
            variant="outline"
            size="sm"
            @click="handleBatchRetryOcr"
          >
            <RotateCw class="size-3.5" />
            批量重试OCR
          </Button>
          <Button v-if="canManage" variant="outline" size="sm" @click="configOpen = true">
            <Settings2 class="size-3.5" />
            知识库配置
          </Button>
        </div>
      </div>

      <UploadZone v-if="canManage" class="mb-4" @files-selected="handleFilesSelected" />

      <DocumentTable
        :docs="docs"
        :loading="docsLoading"
        :can-manage="canManage"
        @retry="handleRetry"
        @delete="(doc) => (deleteTarget = doc)"
      />
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

  <KbConfigDialog v-model:open="configOpen" :kb="currentKb" @saved="loadKbs" />

  <!-- 删除知识库确认 Dialog -->
  <Dialog :open="!!kbDeleteTarget" @update:open="(v) => { if (!v) kbDeleteTarget = null; }">
    <DialogContent>
      <DialogHeader>
        <DialogTitle>删除知识库</DialogTitle>
        <DialogDescription>
          将删除「{{ kbDeleteTarget?.name }}」及其
          {{ kbDeleteTarget ? (docCounts[kbDeleteTarget.id] ?? 0) : 0 }}
          篇文档与相关会话，不可恢复。
        </DialogDescription>
      </DialogHeader>
      <DialogFooter>
        <Button variant="outline" @click="kbDeleteTarget = null">取消</Button>
        <Button variant="destructive" :disabled="deletingKb" @click="confirmDeleteKb">确认删除</Button>
      </DialogFooter>
    </DialogContent>
  </Dialog>
</template>
