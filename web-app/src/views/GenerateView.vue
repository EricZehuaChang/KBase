<script setup lang="ts">
// 生成页：KB 选择 + Tabs（方案生成/定期汇编）+ 任务历史（该 KB 的 jobs 列表，
// 点击加载其产物预览）。ProposalWizard/DigestPanel 各自持有向导内部状态，
// 用 :key="kbId" 在切换知识库时整体重建，避免残留上一个 KB 的表单/大纲状态。
import { computed, onMounted, ref, watch } from "vue";
import {
  Tabs, TabsContent, TabsList, TabsTrigger,
} from "@/components/ui/tabs";
import {
  Select, SelectContent, SelectGroup, SelectItem, SelectTrigger, SelectValue,
} from "@/components/ui/select";
import { Badge } from "@/components/ui/badge";
import ProposalWizard from "@/components/ProposalWizard.vue";
import DigestPanel from "@/components/DigestPanel.vue";
import JobProgress from "@/components/JobProgress.vue";
import { listKbs, listJobs, listProviders, type Kb, type Job } from "@/lib/api";
import { jobStatusBadge, jobTypeLabel, jobHasArtifact } from "@/lib/generate-utils";

const kbs = ref<Kb[]>([]);
const kbId = ref<string | undefined>(undefined);
const providers = ref<string[]>([]);
const tab = ref<"proposal" | "digest">("proposal");

const jobs = ref<Job[]>([]);
const historyOpenId = ref<string | undefined>(undefined);

async function loadJobs() {
  if (!kbId.value) return;
  jobs.value = await listJobs(kbId.value);
}

onMounted(async () => {
  const [kbList, providerList] = await Promise.all([listKbs(), listProviders()]);
  kbs.value = kbList;
  providers.value = providerList.providers;
  if (kbs.value.length) kbId.value = kbs.value[0].id;
});

watch(kbId, async (id) => {
  historyOpenId.value = undefined;
  if (id) await loadJobs();
}, { immediate: true });

function openHistory(job: Job) {
  if (!jobHasArtifact(job.status)) return;
  historyOpenId.value = historyOpenId.value === job.id ? undefined : job.id;
}

const currentKbName = computed(() => kbs.value.find((k) => k.id === kbId.value)?.name);
</script>

<template>
  <div class="flex h-full flex-col">
    <header class="flex h-14 shrink-0 items-center gap-3 border-b border-[var(--border)] px-4">
      <Select v-model="kbId">
        <SelectTrigger class="w-48"><SelectValue placeholder="选择知识库" /></SelectTrigger>
        <SelectContent>
          <SelectGroup>
            <SelectItem v-for="kb in kbs" :key="kb.id" :value="kb.id">{{ kb.name }}</SelectItem>
          </SelectGroup>
        </SelectContent>
      </Select>
    </header>

    <div class="flex-1 overflow-y-auto p-6">
      <div v-if="!kbId" class="py-12 text-center text-sm text-[var(--text-3)]">
        请先选择知识库
      </div>

      <template v-else>
        <Tabs v-model="tab">
          <TabsList>
            <TabsTrigger value="proposal">方案生成</TabsTrigger>
            <TabsTrigger value="digest">定期汇编</TabsTrigger>
          </TabsList>

          <TabsContent value="proposal" class="mt-4">
            <ProposalWizard :key="kbId" :kb-id="kbId" :providers="providers" @job-created="loadJobs" />
          </TabsContent>
          <TabsContent value="digest" class="mt-4">
            <DigestPanel :key="kbId" :kb-id="kbId" @job-created="loadJobs" />
          </TabsContent>
        </Tabs>

        <section class="mt-10 max-w-2xl">
          <h2 class="mb-3 text-sm font-medium text-[var(--text-2)]">
            任务历史{{ currentKbName ? `（${currentKbName}）` : "" }}
          </h2>
          <p v-if="!jobs.length" class="text-sm text-[var(--text-3)]">暂无任务</p>
          <ul v-else class="flex flex-col gap-2">
            <li
              v-for="job in jobs" :key="job.id"
              class="rounded-[var(--radius-card)] border border-[var(--border)] bg-[var(--surface)] p-3"
            >
              <button
                type="button"
                class="flex w-full items-center justify-between gap-2 text-left"
                :class="jobHasArtifact(job.status) ? 'cursor-pointer' : 'cursor-default'"
                @click="openHistory(job)"
              >
                <span class="flex items-center gap-2 text-sm">
                  <span class="font-medium">{{ jobTypeLabel(job.type) }}</span>
                  <Badge :class="jobStatusBadge(job.status).class">{{ jobStatusBadge(job.status).label }}</Badge>
                </span>
                <span class="text-xs text-[var(--text-3)]">{{ job.updated_at }}</span>
              </button>
              <div v-if="historyOpenId === job.id" class="mt-3">
                <JobProgress :job-id="job.id" />
              </div>
            </li>
          </ul>
        </section>
      </template>
    </div>
  </div>
</template>
