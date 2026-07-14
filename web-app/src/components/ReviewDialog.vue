<script setup lang="ts">
// VLM 识别校验对话框（F）：左侧原图预览、右侧识别 Markdown 可编辑——
// 管理员对照修正（VLM 幻觉在此拦截）后「确认入库」，此刻才分块向量化。
import { ref, watch } from "vue";
import { toast } from "vue-sonner";
import { CheckCircle2 } from "@lucide/vue";
import { Button } from "@/components/ui/button";
import {
  Dialog, DialogContent, DialogHeader, DialogTitle, DialogDescription, DialogFooter,
} from "@/components/ui/dialog";
import {
  docOriginalUrl, getDocContent, reviewDocument, type DocumentItem,
} from "@/lib/api";

const props = defineProps<{ open: boolean; doc: DocumentItem | null }>();
const emit = defineEmits<{ "update:open": [value: boolean]; approved: [] }>();

const markdown = ref("");
const loading = ref(false);
const saving = ref(false);
const loadError = ref<string | null>(null);

watch(() => props.open, async (isOpen) => {
  if (!isOpen || !props.doc) return;
  loading.value = true;
  loadError.value = null;
  markdown.value = "";
  try {
    markdown.value = (await getDocContent(props.doc.id)).markdown;
  } catch (err) {
    loadError.value = err instanceof Error ? err.message : String(err);
  } finally {
    loading.value = false;
  }
});

async function approve() {
  if (!props.doc || !markdown.value.trim()) return;
  saving.value = true;
  try {
    await reviewDocument(props.doc.id, markdown.value);
    toast.success("已确认入库，开始向量化");
    emit("update:open", false);
    emit("approved");
  } catch (err) {
    toast.error(err instanceof Error ? err.message : String(err));
  } finally {
    saving.value = false;
  }
}
</script>

<template>
  <Dialog :open="open" @update:open="(v) => emit('update:open', v)">
    <DialogContent class="max-h-[92vh] max-w-5xl overflow-hidden">
      <DialogHeader>
        <DialogTitle>校验识别结果：{{ doc?.filename }}</DialogTitle>
        <DialogDescription>
          对照左侧原图核对右侧识别文本（可直接修改），确认后才会向量化入库
        </DialogDescription>
      </DialogHeader>

      <div class="grid max-h-[68vh] grid-cols-2 gap-4">
        <!-- 左：原图预览（同源带 Cookie 的 inline 直链） -->
        <div class="overflow-auto rounded-[var(--radius-ctl)] border border-[var(--border)] bg-[var(--surface-2)] p-2">
          <img
            v-if="doc"
            :src="docOriginalUrl(doc.id, { inline: true })"
            :alt="doc.filename"
            class="max-w-full"
          />
        </div>
        <!-- 右：识别 Markdown 可编辑 -->
        <div class="flex flex-col">
          <p v-if="loading" class="text-sm text-[var(--text-3)]">加载识别结果…</p>
          <p v-else-if="loadError" class="text-sm text-[var(--err)]">⚠️ {{ loadError }}</p>
          <textarea
            v-else
            v-model="markdown"
            class="h-full min-h-[50vh] flex-1 resize-none rounded-[var(--radius-ctl)] border border-[var(--border)] bg-[var(--surface)] px-3 py-2 font-mono text-sm leading-relaxed text-[var(--text)] outline-none focus:border-[var(--accent)]"
          />
        </div>
      </div>

      <DialogFooter>
        <Button variant="outline" @click="emit('update:open', false)">稍后处理</Button>
        <Button :disabled="saving || loading || !markdown.trim()" @click="approve">
          <CheckCircle2 class="size-3.5" />
          确认入库
        </Button>
      </DialogFooter>
    </DialogContent>
  </Dialog>
</template>
