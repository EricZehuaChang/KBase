<script setup lang="ts">
// 分块管理 Dialog（M6-1）：查看文档分块、按文本过滤定位坏块、启停开关
// （停用=从检索索引摘除，可恢复）、编辑文本（叶子重嵌入+重索引，父块仅
// 落库）。分页 50/页；操作即时生效并回写行内状态。
import { ref, watch } from "vue";
import { useI18n } from "vue-i18n";
import { toast } from "vue-sonner";
import { Pencil, Search } from "@lucide/vue";
import { Badge } from "@/components/ui/badge";
import { Button } from "@/components/ui/button";
import { Input } from "@/components/ui/input";
import { Switch } from "@/components/ui/switch";
import {
  Dialog, DialogContent, DialogHeader, DialogTitle, DialogDescription, DialogFooter,
} from "@/components/ui/dialog";
import {
  listDocChunks, updateChunk, type ChunkItem, type DocumentItem,
} from "@/lib/api";

const props = defineProps<{ open: boolean; doc: DocumentItem | null }>();
const emit = defineEmits<{ "update:open": [value: boolean] }>();
const { t } = useI18n();

const PAGE_SIZE = 50;
const items = ref<ChunkItem[]>([]);
const total = ref(0);
const offset = ref(0);
const q = ref("");
const loading = ref(false);
const busyIds = ref<Set<string>>(new Set());

// 编辑子对话框
const editTarget = ref<ChunkItem | null>(null);
const editText = ref("");
const savingEdit = ref(false);

async function load() {
  if (!props.doc) return;
  loading.value = true;
  try {
    const page = await listDocChunks(props.doc.id, {
      offset: offset.value, limit: PAGE_SIZE, q: q.value.trim() || undefined,
    });
    items.value = page.items;
    total.value = page.total;
  } catch (err) {
    toast.error(err instanceof Error ? err.message : String(err));
  } finally {
    loading.value = false;
  }
}

watch(() => props.open, (isOpen) => {
  if (!isOpen) return;
  offset.value = 0;
  q.value = "";
  items.value = [];
  load();
});

function searchChunks() {
  offset.value = 0;
  load();
}

async function toggleEnabled(chunk: ChunkItem, enabled: boolean) {
  busyIds.value.add(chunk.id);
  try {
    const updated = await updateChunk(chunk.id, { enabled });
    Object.assign(chunk, updated);
    toast.success(enabled ? t("chunk.enabled_toast") : t("chunk.disabled_toast"));
  } catch (err) {
    toast.error(err instanceof Error ? err.message : String(err));
  } finally {
    busyIds.value.delete(chunk.id);
  }
}

function openEdit(chunk: ChunkItem) {
  editTarget.value = chunk;
  editText.value = chunk.text;
}

async function saveEdit() {
  if (!editTarget.value || !editText.value.trim()) return;
  savingEdit.value = true;
  try {
    const updated = await updateChunk(editTarget.value.id, { text: editText.value });
    const row = items.value.find((i) => i.id === updated.id);
    if (row) Object.assign(row, updated);
    toast.success(editTarget.value.is_leaf ? t("chunk.saved_leaf") : t("chunk.saved_parent"));
    editTarget.value = null;
  } catch (err) {
    toast.error(err instanceof Error ? err.message : String(err));
  } finally {
    savingEdit.value = false;
  }
}

function prevPage() {
  offset.value = Math.max(0, offset.value - PAGE_SIZE);
  load();
}

function nextPage() {
  if (offset.value + PAGE_SIZE < total.value) {
    offset.value += PAGE_SIZE;
    load();
  }
}
</script>

<template>
  <Dialog :open="open" @update:open="(v) => emit('update:open', v)">
    <DialogContent class="max-h-[90vh] max-w-3xl overflow-hidden">
      <DialogHeader>
        <DialogTitle>{{ t("chunk.title", { name: doc?.filename }) }}</DialogTitle>
        <DialogDescription>
          {{ t("chunk.desc", { total }) }}
        </DialogDescription>
      </DialogHeader>

      <div class="flex items-center gap-2">
        <Input v-model="q" :placeholder="t('chunk.filter_ph')" @keydown.enter="searchChunks" />
        <Button variant="outline" size="sm" @click="searchChunks">
          <Search class="size-3.5" />
          {{ t("chunk.filter") }}
        </Button>
      </div>

      <div class="max-h-[52vh] overflow-y-auto">
        <p v-if="loading" class="py-6 text-center text-sm text-[var(--text-3)]">{{ t("common.loading") }}</p>
        <p v-else-if="items.length === 0" class="py-6 text-center text-sm text-[var(--text-3)]">
          {{ t("chunk.no_match") }}
        </p>
        <div
          v-for="chunk in items"
          v-else
          :key="chunk.id"
          class="border-b border-[var(--border)] py-3"
          :class="chunk.enabled ? '' : 'opacity-50'"
        >
          <div class="flex items-center gap-2">
            <Badge :class="chunk.is_leaf
              ? 'bg-[var(--accent-weak)] text-[var(--accent-text)]'
              : 'bg-[var(--surface-2)] text-[var(--text-2)]'">
              {{ chunk.is_leaf ? t("chunk.leaf") : t("chunk.parent") }}
            </Badge>
            <span class="truncate text-xs text-[var(--text-3)]">{{ chunk.heading_path }}</span>
            <span v-if="chunk.page" class="text-xs text-[var(--text-3)]">· {{ t("msg.page", { page: chunk.page }) }}</span>
            <span class="text-xs text-[var(--text-3)]">· {{ t("chunk.chars", { count: chunk.chars }) }}</span>
            <div class="ml-auto flex items-center gap-2">
              <Button variant="ghost" size="icon-sm" :aria-label="t('chunk.edit')" @click="openEdit(chunk)">
                <Pencil class="size-3.5" />
              </Button>
              <Switch
                :model-value="chunk.enabled"
                :disabled="busyIds.has(chunk.id)"
                :aria-label="chunk.enabled ? t('chunk.disable') : t('chunk.enable')"
                @update:model-value="(v: boolean) => toggleEnabled(chunk, v)"
              />
            </div>
          </div>
          <p class="mt-1 line-clamp-3 whitespace-pre-wrap text-sm text-[var(--text-2)]">
            {{ chunk.text }}
          </p>
        </div>
      </div>

      <DialogFooter class="items-center">
        <span class="mr-auto text-xs text-[var(--text-3)]">
          {{ total === 0 ? 0 : offset + 1 }}-{{ Math.min(offset + PAGE_SIZE, total) }} / {{ total }}
        </span>
        <Button variant="outline" size="sm" :disabled="offset === 0" @click="prevPage">{{ t("chunk.prev_page") }}</Button>
        <Button variant="outline" size="sm" :disabled="offset + PAGE_SIZE >= total" @click="nextPage">
          {{ t("chunk.next_page") }}
        </Button>
      </DialogFooter>
    </DialogContent>
  </Dialog>

  <!-- 编辑分块子对话框 -->
  <Dialog :open="!!editTarget" @update:open="(v) => { if (!v) editTarget = null; }">
    <DialogContent class="max-w-2xl">
      <DialogHeader>
        <DialogTitle>{{ t("chunk.edit_title") }}</DialogTitle>
        <DialogDescription>
          {{ editTarget?.is_leaf ? t("chunk.edit_desc_leaf") : t("chunk.edit_desc_parent") }}
        </DialogDescription>
      </DialogHeader>
      <textarea
        v-model="editText"
        rows="10"
        class="rounded-[var(--radius-ctl)] border border-[var(--border)] bg-[var(--surface)] px-3 py-2 text-sm text-[var(--text)] outline-none focus:border-[var(--accent)]"
      />
      <DialogFooter>
        <Button variant="outline" @click="editTarget = null">{{ t("common.cancel") }}</Button>
        <Button :disabled="savingEdit || !editText.trim()" @click="saveEdit">{{ t("common.save") }}</Button>
      </DialogFooter>
    </DialogContent>
  </Dialog>
</template>
