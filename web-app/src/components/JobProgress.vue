<script setup lang="ts">
// 任务进度：轮询单个 job（useJob，3s 间隔，终态自动停止）展示步骤清单
// （✓/✗/转圈图标）；到达 done/done_with_errors 后拉取 md 产物做纯文本预览
// （与 CitationDrawer 全文预览同风格：<pre> + whitespace-pre-wrap，不经
// v-html）+ md/docx 下载直链。
import { computed, onBeforeUnmount, ref, watch, type Ref } from "vue";
import { useI18n } from "vue-i18n";
import { CheckCircle2, XCircle, Loader2, Circle, Download, FileDown } from "@lucide/vue";
import { Button } from "@/components/ui/button";
import { useJob } from "@/composables/useJob";
import { artifactUrl, type JobStepStatus } from "@/lib/api";
import { jobHasArtifact } from "@/lib/generate-utils";

const props = defineProps<{ jobId: string | undefined }>();
const { t } = useI18n();

const jobId: Ref<string | undefined> = computed(() => props.jobId) as unknown as Ref<string | undefined>;
const { job, error: jobError, start, stopPolling } = useJob(jobId);

const preview = ref<string | null>(null);
const previewLoading = ref(false);
const previewError = ref<string | null>(null);

const isTerminal = computed(() =>
  !!job.value && ["done", "done_with_errors", "failed"].includes(job.value.status));
const hasArtifact = computed(() => !!job.value && jobHasArtifact(job.value.status));

async function loadPreview(id: string) {
  preview.value = null;
  previewError.value = null;
  previewLoading.value = true;
  try {
    const res = await fetch(artifactUrl(id, "md"));
    if (!res.ok) throw new Error(t("job.artifact_load_failed", { status: res.status }));
    preview.value = await res.text();
  } catch (err) {
    previewError.value = err instanceof Error ? err.message : String(err);
  } finally {
    previewLoading.value = false;
  }
}

watch(() => props.jobId, async (id) => {
  preview.value = null;
  previewError.value = null;
  if (id) await start();
}, { immediate: true });

watch(hasArtifact, async (ready) => {
  if (ready && props.jobId) await loadPreview(props.jobId);
});

onBeforeUnmount(stopPolling);

function stepIcon(status: JobStepStatus) {
  if (status === "done") return CheckCircle2;
  if (status === "failed") return XCircle;
  if (status === "running") return Loader2;
  return Circle;
}

function stepIconClass(status: JobStepStatus) {
  if (status === "done") return "text-[var(--ok)]";
  if (status === "failed") return "text-[var(--err)]";
  if (status === "running") return "text-[var(--accent-text)] animate-spin";
  return "text-[var(--text-3)]";
}
</script>

<template>
  <div class="flex flex-col gap-4">
    <p v-if="jobError" class="text-sm text-[var(--err)]">⚠️ {{ jobError }}</p>

    <ul v-if="job?.progress?.steps.length" class="flex flex-col gap-1.5">
      <li
        v-for="step in job.progress.steps"
        :key="step.name"
        class="flex items-center gap-2 text-sm"
      >
        <component :is="stepIcon(step.status)" class="size-4 shrink-0" :class="stepIconClass(step.status)" />
        <span :class="step.status === 'failed' ? 'text-[var(--err)]' : 'text-[var(--text)]'">{{ step.name }}</span>
        <span v-if="step.status === 'failed' && step.detail" class="text-xs text-[var(--text-3)]">
          （{{ step.detail }}）
        </span>
      </li>
    </ul>

    <div v-if="isTerminal" class="text-sm">
      <span v-if="job?.status === 'done'" class="text-[var(--ok)]">{{ t("job.done") }}</span>
      <span v-else-if="job?.status === 'done_with_errors'" class="text-[var(--warn)]">{{ t("job.done_with_errors") }}</span>
      <span v-else-if="job?.status === 'failed'" class="text-[var(--err)]">{{ t("job.failed") }}{{ job?.error ? `：${job.error}` : "" }}</span>
    </div>

    <div v-if="hasArtifact" class="flex flex-col gap-3">
      <div class="flex gap-2">
        <Button as-child size="sm" variant="outline">
          <a :href="artifactUrl(props.jobId!, 'md')" download>
            <Download class="size-3.5" />
            {{ t("job.download_md") }}
          </a>
        </Button>
        <Button as-child size="sm" variant="outline">
          <a :href="artifactUrl(props.jobId!, 'docx')" download>
            <FileDown class="size-3.5" />
            {{ t("job.download_word") }}
          </a>
        </Button>
      </div>

      <div class="max-h-[50vh] overflow-y-auto rounded-[var(--radius-card)] border border-[var(--border)] bg-[var(--surface)] p-4 text-sm leading-relaxed">
        <p v-if="previewLoading" class="text-[var(--text-3)]">{{ t("job.loading_artifact") }}</p>
        <p v-else-if="previewError" class="text-[var(--err)]">⚠️ {{ previewError }}</p>
        <pre v-else-if="preview" class="whitespace-pre-wrap font-sans">{{ preview }}</pre>
      </div>
    </div>
  </div>
</template>
