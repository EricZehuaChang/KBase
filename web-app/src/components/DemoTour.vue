<script setup lang="ts">
// 产品导览悬浮卡（portal + admin 各挂一个实例，app prop 标识宿主端）：
// 步骤见 lib/demo-tour.ts。无遮罩——页面全程可交互（录屏/跟练场景，
// 卡片是提词器不是牢笼）；跨端步骤整页跳转，localStorage 续步。
// 键盘：← → 翻步，Esc 最小化到右侧胶囊。
import { computed, onBeforeUnmount, onMounted, watch } from "vue";
import { useRoute, useRouter } from "vue-router";
import { useI18n } from "vue-i18n";
import { ChevronLeft, ChevronRight, MousePointerClick, Play, X } from "@lucide/vue";
import { ref } from "vue";
import {
  TOUR_STEPS, loadTourState, samePath, saveTourState, startTour as _start,
  stepHref, stopTour, tourActive, tourStep,
} from "@/lib/demo-tour";

const props = defineProps<{ app: "portal" | "admin"; enabled?: boolean }>();
const route = useRoute();
const router = useRouter();
const { t } = useI18n();

const minimized = ref(false);
const cur = computed(() => TOUR_STEPS[tourStep.value]);
const isFirst = computed(() => tourStep.value === 0);
const isLast = computed(() => tourStep.value === TOUR_STEPS.length - 1);

function navigateTo(idx: number) {
  const step = TOUR_STEPS[idx];
  if (step.app === props.app) {
    // 同端：SPA 内导航（带 ?tab= 查询串原样交给 router）
    if (!samePath(route.path, step) || step.path.includes("?")) {
      void router.push(step.path);
    }
  } else {
    // 跨端：先落盘再整页跳转，落地端 DemoTour 恢复状态续步
    saveTourState();
    window.location.href = stepHref(step);
  }
}

function goTo(idx: number) {
  if (idx < 0) return;
  if (idx >= TOUR_STEPS.length) { stopTour(); return; }
  tourStep.value = idx;
  saveTourState();
  minimized.value = false;
  navigateTo(idx);
}

// UserMenu 调 startTour() 后由本 watcher 负责把页面带到第一步
watch(tourActive, (on) => { if (on) navigateTo(tourStep.value); });

function onKey(e: KeyboardEvent) {
  if (!tourActive.value || minimized.value) return;
  if (e.key === "Escape") minimized.value = true;
  else if (e.key === "ArrowRight") goTo(tourStep.value + 1);
  else if (e.key === "ArrowLeft") goTo(tourStep.value - 1);
}

onMounted(() => {
  loadTourState();
  // 跨端落地/刷新恢复：若当前端持有当前步且路由不在位，纠位
  if (tourActive.value && cur.value.app === props.app
      && !samePath(route.path, cur.value)) {
    void router.push(cur.value.path);
  }
  window.addEventListener("keydown", onKey);
});
onBeforeUnmount(() => window.removeEventListener("keydown", onKey));
</script>

<template>
  <template v-if="tourActive && props.enabled && cur.app === props.app">
    <!-- 最小化胶囊（右侧竖排） -->
    <button
      v-if="minimized"
      type="button"
      class="fixed right-0 top-1/2 z-50 flex -translate-y-1/2 flex-col items-center gap-1.5 rounded-l-xl bg-[var(--accent)] px-2.5 py-4 text-white shadow-lg transition-colors hover:opacity-90"
      @click="minimized = false"
    >
      <Play class="size-4" />
      <span class="rounded-full bg-white/20 px-1.5 py-0.5 text-[10px] font-bold">
        {{ tourStep + 1 }}/{{ TOUR_STEPS.length }}
      </span>
    </button>

    <!-- 导览卡：底部居中，无遮罩，页面可继续操作 -->
    <div
      v-else
      class="fixed bottom-5 left-1/2 z-50 w-[400px] max-w-[calc(100vw-24px)] -translate-x-1/2 overflow-hidden rounded-xl border border-[var(--border)] bg-[var(--surface)] shadow-[var(--shadow-drawer)]"
    >
      <div class="flex items-center justify-between bg-[var(--accent)] px-3.5 py-2">
        <span class="text-xs font-semibold text-white">
          {{ t("tour.ui.badge") }} · {{ tourStep + 1 }}/{{ TOUR_STEPS.length }}
        </span>
        <div class="flex items-center gap-1">
          <button
            type="button" class="rounded p-0.5 text-white/70 hover:text-white"
            :title="t('tour.ui.minimize')" @click="minimized = true"
          >
            <ChevronRight class="size-3.5 rotate-90" />
          </button>
          <button
            type="button" class="rounded p-0.5 text-white/70 hover:text-white"
            :title="t('tour.ui.exit')" @click="stopTour()"
          >
            <X class="size-3.5" />
          </button>
        </div>
      </div>

      <div class="max-h-[38vh] space-y-1.5 overflow-y-auto px-3.5 py-2.5">
        <h3 class="text-[13px] font-bold leading-snug">{{ t(`tour.${cur.key}.title`) }}</h3>
        <p class="whitespace-pre-line text-xs leading-relaxed text-[var(--text-2)]">
          {{ t(`tour.${cur.key}.desc`) }}
        </p>
        <div
          v-if="cur.action"
          class="flex items-start gap-1.5 rounded-lg border border-[var(--warn)] bg-[var(--warn-weak)] px-2.5 py-1.5"
        >
          <MousePointerClick class="mt-0.5 size-3 shrink-0 text-[var(--warn)]" />
          <p class="text-[11px] font-medium text-[var(--text)]">{{ t(`tour.${cur.key}.action`) }}</p>
        </div>
      </div>

      <div class="flex items-center justify-between border-t border-[var(--border)] px-3.5 py-2">
        <button
          type="button"
          class="flex items-center gap-1 rounded-lg px-2 py-1 text-[11px] font-medium transition-colors"
          :class="isFirst ? 'cursor-not-allowed text-[var(--text-3)]' : 'text-[var(--text-2)] hover:bg-[var(--surface-2)]'"
          :disabled="isFirst"
          @click="goTo(tourStep - 1)"
        >
          <ChevronLeft class="size-3" /> {{ t("tour.ui.prev") }}
        </button>
        <div class="flex gap-1">
          <div
            v-for="(s, i) in TOUR_STEPS" :key="s.key"
            class="rounded-full transition-all"
            :class="i === tourStep ? 'h-1.5 w-4 bg-[var(--accent)]' : 'h-1.5 w-1.5 bg-[var(--border)]'"
          />
        </div>
        <button
          type="button"
          class="flex items-center gap-1 rounded-lg bg-[var(--accent)] px-2.5 py-1 text-[11px] font-medium text-white transition-opacity hover:opacity-90"
          @click="goTo(tourStep + 1)"
        >
          {{ isLast ? t("tour.ui.finish") : t("tour.ui.next") }}
          <ChevronRight class="size-3" />
        </button>
      </div>
    </div>
  </template>
</template>
