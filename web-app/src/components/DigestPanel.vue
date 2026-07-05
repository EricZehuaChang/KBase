<script setup lang="ts">
// 定期汇编面板：ready 文档多选（checkbox + 全选）→ 生成汇编 → JobProgress。
// 不选文档时 doc_ids 传 undefined（=全库 ready 文档，后端 digest.py 语义）。
import { computed, onMounted, ref, watch } from "vue";
import { Button } from "@/components/ui/button";
import JobProgress from "@/components/JobProgress.vue";
import { createJob, listDocs, type DocumentItem } from "@/lib/api";

const props = defineProps<{ kbId: string }>();
const emit = defineEmits<{ jobCreated: [] }>();

const docs = ref<DocumentItem[]>([]);
const readyDocs = computed(() => docs.value.filter((d) => d.status === "ready"));
const selected = ref<Set<string>>(new Set());
const allSelected = computed(() => readyDocs.value.length > 0 && selected.value.size === readyDocs.value.length);

async function loadDocs() {
  docs.value = await listDocs(props.kbId);
  selected.value = new Set();
}

onMounted(loadDocs);
watch(() => props.kbId, loadDocs);

function toggle(docId: string) {
  const next = new Set(selected.value);
  if (next.has(docId)) next.delete(docId);
  else next.add(docId);
  selected.value = next;
}

function toggleAll() {
  selected.value = allSelected.value ? new Set() : new Set(readyDocs.value.map((d) => d.id));
}

const jobId = ref<string | undefined>(undefined);
const createError = ref<string | null>(null);
const submitting = ref(false);

async function handleGenerate() {
  createError.value = null;
  submitting.value = true;
  try {
    const docIds = selected.value.size ? [...selected.value] : undefined;
    const { id } = await createJob({
      type: "digest", kb_id: props.kbId, params: docIds ? { doc_ids: docIds } : {},
    });
    jobId.value = id;
    emit("jobCreated");
  } catch (err) {
    createError.value = err instanceof Error ? err.message : String(err);
  } finally {
    submitting.value = false;
  }
}
</script>

<template>
  <div class="flex flex-col gap-4 max-w-2xl">
    <div v-if="!jobId" class="flex flex-col gap-3">
      <p v-if="!readyDocs.length" class="text-sm text-[var(--text-3)]">
        该知识库暂无就绪文档
      </p>
      <template v-else>
        <label class="flex items-center gap-2 text-sm font-medium">
          <input type="checkbox" :checked="allSelected" aria-label="全选" @change="toggleAll">
          全选（{{ selected.size }}/{{ readyDocs.length }}）
        </label>
        <ul class="flex flex-col gap-1.5 max-h-72 overflow-y-auto rounded-[var(--radius-card)] border border-[var(--border)] p-2">
          <li v-for="doc in readyDocs" :key="doc.id">
            <label class="flex items-center gap-2 text-sm">
              <input
                type="checkbox" :checked="selected.has(doc.id)"
                :aria-label="doc.filename" @change="toggle(doc.id)"
              >
              {{ doc.filename }}
            </label>
          </li>
        </ul>
      </template>

      <p v-if="createError" class="text-sm text-[var(--err)]">⚠️ {{ createError }}</p>

      <Button class="self-start" :disabled="submitting || !readyDocs.length" @click="handleGenerate">
        {{ submitting ? "提交中…" : "生成汇编" }}
      </Button>
    </div>

    <JobProgress v-else :job-id="jobId" />
  </div>
</template>
