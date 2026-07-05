<script setup lang="ts">
// 方案生成向导：step1 表单（KB 已由父组件选定，主题/要求/provider）→ 生成大纲
// →step2 OutlineEditor → 开始生成 → step3 JobProgress。步骤态用 step ref
// 本地持有，切换 KB 时父组件销毁/重建本组件实例即可重置（见 GenerateView
// :key="kbId"），故这里不必监听 kbId 变化。
import { ref } from "vue";
import { Button } from "@/components/ui/button";
import {
  Select, SelectContent, SelectGroup, SelectItem, SelectTrigger, SelectValue,
} from "@/components/ui/select";
import OutlineEditor from "@/components/OutlineEditor.vue";
import JobProgress from "@/components/JobProgress.vue";
import { generateOutline, createJob, type OutlineSection } from "@/lib/api";

const props = defineProps<{ kbId: string; providers: string[]; canManage?: boolean }>();
const emit = defineEmits<{ jobCreated: [] }>();

const step = ref<1 | 2 | 3>(1);

const topic = ref("");
const requirements = ref("");
const provider = ref<string | undefined>(undefined);
const outlineLoading = ref(false);
const outlineError = ref<string | null>(null);
const sections = ref<OutlineSection[]>([]);

async function handleGenerateOutline() {
  if (!topic.value.trim()) return;
  outlineLoading.value = true;
  outlineError.value = null;
  try {
    sections.value = await generateOutline(props.kbId, topic.value.trim(), requirements.value, provider.value);
    step.value = 2;
  } catch (err) {
    outlineError.value = err instanceof Error ? err.message : String(err);
  } finally {
    outlineLoading.value = false;
  }
}

const jobId = ref<string | undefined>(undefined);
const createError = ref<string | null>(null);

async function handleStartGenerate() {
  createError.value = null;
  try {
    const { id } = await createJob({
      type: "proposal", kb_id: props.kbId, provider: provider.value,
      params: { topic: topic.value.trim(), requirements: requirements.value, outline: sections.value },
    });
    jobId.value = id;
    step.value = 3;
    emit("jobCreated");
  } catch (err) {
    createError.value = err instanceof Error ? err.message : String(err);
  }
}

function backToStep1() {
  step.value = 1;
}
</script>

<template>
  <div class="flex flex-col gap-4">
    <div v-if="step === 1" class="flex flex-col gap-3 max-w-xl">
      <label class="flex flex-col gap-1 text-sm">
        主题
        <input
          v-model="topic" type="text" placeholder="例如：某师市人才引进住房保障实施方案"
          class="h-9 rounded-[var(--radius-ctl)] border border-[var(--border)] bg-transparent px-2.5 text-sm outline-none focus-visible:border-[var(--accent)]"
        >
      </label>
      <label class="flex flex-col gap-1 text-sm">
        要求
        <textarea
          v-model="requirements" rows="4" placeholder="补充撰写要求，如需依据的政策、篇幅、侧重点等"
          class="resize-none rounded-[var(--radius-ctl)] border border-[var(--border)] bg-transparent px-2.5 py-1.5 text-sm outline-none focus-visible:border-[var(--accent)]"
        />
      </label>
      <label v-if="providers.length" class="flex flex-col gap-1 text-sm">
        Provider
        <Select v-model="provider">
          <SelectTrigger class="w-48"><SelectValue placeholder="默认（active）" /></SelectTrigger>
          <SelectContent>
            <SelectGroup>
              <SelectItem v-for="p in providers" :key="p" :value="p">{{ p }}</SelectItem>
            </SelectGroup>
          </SelectContent>
        </Select>
      </label>

      <p v-if="outlineError" class="text-sm text-[var(--err)]">⚠️ {{ outlineError }}</p>

      <Button
        v-if="canManage ?? true"
        class="self-start" :disabled="outlineLoading || !topic.trim()" @click="handleGenerateOutline"
      >
        {{ outlineLoading ? "生成中…" : "生成大纲" }}
      </Button>
    </div>

    <div v-else-if="step === 2" class="flex flex-col gap-4 max-w-2xl">
      <OutlineEditor v-model="sections" />
      <p v-if="createError" class="text-sm text-[var(--err)]">⚠️ {{ createError }}</p>
      <div v-if="canManage ?? true" class="flex gap-2">
        <Button variant="outline" @click="backToStep1">上一步</Button>
        <Button :disabled="!sections.length" @click="handleStartGenerate">开始生成</Button>
      </div>
    </div>

    <div v-else class="max-w-2xl">
      <JobProgress :job-id="jobId" />
    </div>
  </div>
</template>
