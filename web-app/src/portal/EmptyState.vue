<script setup lang="ts">
// 【使用端】问答页空会话状态：新会话/新用户看到的第一屏。
//
// 快捷问题按**当前库的真实文档**动态生成（文档名→问句模板），点了必然
// 检索命中——此前是写死的四条旧评测语料（住宿费/公务卡那批），换个库
// 就全部答不上，误导新用户（生产 UX 反馈）。库为空/加载失败时不显示
// chips，只留引导文案（没有内容时给会拒答的假问题更糟）。
import { onMounted, ref, watch } from "vue";
import { MessageSquare } from "@lucide/vue";
import { useI18n } from "vue-i18n";
import { listDocs } from "@/lib/api";
import { kbId } from "./topbar-state";

const { t } = useI18n();
const emit = defineEmits<{ pick: [question: string] }>();

const questions = ref<string[]>([]);

function docTitle(filename: string): string {
  return filename.replace(/\.[^.]+$/, "");
}

async function buildQuestions() {
  questions.value = [];
  if (!kbId.value) return;
  try {
    const docs = (await listDocs(kbId.value)).filter((d) => d.status === "ready");
    // 每库最多 4 条：前 3 条按文档主题问要点，第 4 条留一条对比/汇总式问法
    const picks = docs.slice(0, 3).map(
      (d) => t("portal.empty.q_main", { title: docTitle(d.filename) }));
    if (docs.length > 1) {
      picks.push(t("portal.empty.q_numbers",
                   { title: docTitle(docs[0].filename) }));
    }
    questions.value = picks;
  } catch {
    questions.value = [];   // 拉取失败只影响 chips，不影响输入框提问
  }
}

onMounted(buildQuestions);
watch(kbId, buildQuestions);
</script>

<template>
  <div class="flex h-full flex-col items-center justify-center gap-6 px-6 text-center">
    <div class="flex flex-col items-center gap-2">
      <MessageSquare class="size-8 text-[var(--text-3)]" />
      <h2 class="text-lg font-medium text-[var(--text)]">{{ t("portal.empty.title") }}</h2>
      <p class="text-sm text-[var(--text-3)]">
        {{ questions.length ? t("portal.empty.hint_questions") : t("portal.empty.hint_empty") }}
      </p>
    </div>

    <div v-if="questions.length" class="flex w-full max-w-lg flex-col gap-2">
      <button
        v-for="q in questions"
        :key="q"
        type="button"
        class="rounded-[var(--radius-ctl)] border border-[var(--border)] bg-[var(--surface)] px-4 py-2.5 text-left text-sm text-[var(--text-2)] transition-colors hover:border-[var(--accent)] hover:text-[var(--text)]"
        @click="emit('pick', q)"
      >
        {{ q }}
      </button>
    </div>
  </div>
</template>
