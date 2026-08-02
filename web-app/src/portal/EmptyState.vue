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
import KnowledgeGlobe from "@/components/KnowledgeGlobe.vue";
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
  <div class="relative flex h-full flex-col items-center justify-center gap-6 px-6 text-center">
    <!-- 3D 知识星球氛围层：低密度低透明度，不抢快捷问题的注意力 -->
    <KnowledgeGlobe :density="0.75" :opacity="0.5" />
    <div class="empty-rise relative z-10 flex flex-col items-center gap-2">
      <MessageSquare class="size-8 text-[var(--text-3)]" />
      <h2 class="text-lg font-medium text-[var(--text)]">{{ t("portal.empty.title") }}</h2>
      <p class="text-sm text-[var(--text-3)]">
        {{ questions.length ? t("portal.empty.hint_questions") : t("portal.empty.hint_empty") }}
      </p>
    </div>

    <div v-if="questions.length" class="relative z-10 flex w-full max-w-lg flex-col gap-2">
      <button
        v-for="(q, i) in questions"
        :key="q"
        type="button"
        data-tour="empty-question"
        class="empty-rise rounded-[var(--radius-ctl)] border border-[var(--border)] bg-[var(--surface)] px-4 py-2.5 text-left text-sm text-[var(--text-2)] transition-all hover:-translate-y-0.5 hover:border-[var(--accent)] hover:text-[var(--text)] hover:shadow-sm"
        :style="{ animationDelay: `${0.08 * (i + 1)}s` }"
        @click="emit('pick', q)"
      >
        {{ q }}
      </button>
    </div>
  </div>
</template>

<style scoped>
/* 入场动效：标题与快捷问题依次淡入上移（动效敏感用户直接呈现终态） */
.empty-rise {
  animation: empty-rise 0.45s ease-out both;
}
@keyframes empty-rise {
  from {
    opacity: 0;
    transform: translateY(10px);
  }
  to {
    opacity: 1;
    transform: translateY(0);
  }
}
@media (prefers-reduced-motion: reduce) {
  .empty-rise {
    animation: none;
  }
}
</style>
