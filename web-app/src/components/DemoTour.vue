<script setup lang="ts">
// 产品导览悬浮卡（portal + admin 各挂一个实例，app prop 标识宿主端）：
// 步骤见 lib/demo-tour.ts。无遮罩——页面全程可交互（录屏/跟练场景，
// 卡片是提词器不是牢笼）；跨端步骤整页跳转，localStorage 续步。
// 键盘：← → 翻步（焦点在输入框时不劫持），Esc 最小化到右侧胶囊。
// 卡片可拖拽（按住顶部色条移动），位置记 localStorage 跨步骤/跨端保持，
// 载入/缩窗时夹回视口；演示目标被卡片挡住时自动上下避让（不落盘）。
// 每步到达后自动执行 demo 演示动作（见 demo-tour.ts DemoOp）：往输入框
// 逐字填示例内容、替用户点击、高亮/聚焦目标——找不到锚点静默跳过。
// 所有运行路径都以 props.enabled（tour_enabled 白名单）为闸：非白名单
// 账号哪怕带着别人留下的 localStorage 导览状态，也不会被幽灵导览拖着走。
import { computed, nextTick, onBeforeUnmount, onMounted, watch } from "vue";
import { useRoute, useRouter } from "vue-router";
import { useI18n } from "vue-i18n";
import {
  ChevronLeft, ChevronRight, GripVertical, MousePointerClick, Play, RotateCcw, X,
} from "@lucide/vue";
import { ref } from "vue";
import {
  type CardPos, type DemoOp,
  TOUR_STEPS, clampPos, loadCardPos, loadTourState, samePath, saveCardPos,
  saveTourState, stepHref, stopTour, tourActive, tourStep,
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
  if (idx >= TOUR_STEPS.length) { stopTour(); clearHighlights(); return; }
  tourStep.value = idx;
  saveTourState();
  minimized.value = false;
  navigateTo(idx);
  void runDemo();
}

/** 恢复续步（挂载/白名单确认后调用）：把页面纠到当前步并跑演示。
 *  enabled 是异步的（/auth/me 返回后才为 true），挂载时通常还是 false，
 *  真正的恢复由下面 watch(props.enabled) 触发。 */
function resumeIfReady() {
  if (!props.enabled || !tourActive.value || cur.value.app !== props.app) return;
  if (!samePath(route.path, cur.value)) void router.push(cur.value.path);
  void runDemo();
}

// UserMenu 调 startTour() 后由本 watcher 负责把页面带到第一步
watch(tourActive, (on) => {
  if (!props.enabled) return;
  if (on) { navigateTo(tourStep.value); void runDemo(); }
  else clearHighlights();
});
watch(() => props.enabled, (on) => { if (on) resumeIfReady(); });

function onKey(e: KeyboardEvent) {
  if (!tourActive.value || !props.enabled || minimized.value) return;
  if (e.key === "Escape") { minimized.value = true; return; }
  // 焦点在输入框里时 ← → 是光标移动，不翻步——演示动作会自动聚焦输入框，
  // 不加这道闸，用户改示例问题时按方向键会莫名跳步（Esc 收起仍保留）
  const el = e.target as HTMLElement | null;
  if (el && (el instanceof HTMLInputElement || el instanceof HTMLTextAreaElement
      || el.isContentEditable)) return;
  if (e.key === "ArrowRight") goTo(tourStep.value + 1);
  else if (e.key === "ArrowLeft") goTo(tourStep.value - 1);
}

function exitTour() {
  stopTour();
  clearHighlights();
}

onMounted(() => {
  loadTourState();
  resumeIfReady();
  window.addEventListener("keydown", onKey);
  window.addEventListener("resize", onResize);
});
onBeforeUnmount(() => {
  window.removeEventListener("keydown", onKey);
  window.removeEventListener("resize", onResize);
  clearHighlights();
});

// ---- 演示动作运行器 ----

// 递增序号做失效令牌：步骤切换/重放会 ++，旧一轮的 await 恢复后发现序号
// 不匹配即放弃（防止上一步的逐字输入继续污染下一步页面）
let runSeq = 0;
const highlighted = new Set<HTMLElement>();

function clearHighlights() {
  runSeq += 1;
  for (const el of highlighted) el.classList.remove("kbase-tour-target");
  highlighted.clear();
}

function findTarget(name: string): HTMLElement | null {
  return document.querySelector<HTMLElement>(`[data-tour="${name}"]`);
}

function sleep(ms: number): Promise<void> {
  return new Promise((r) => setTimeout(r, ms));
}

/** 轮询等锚点出现（路由切换后的懒加载视图要几拍才挂上）；ready 谓词
 *  可再等"出现且可用"（如输入框等到 disabled 解除）。 */
async function waitFor(
  name: string, timeoutMs = 4000, ready?: (el: HTMLElement) => boolean,
): Promise<HTMLElement | null> {
  const deadline = Date.now() + timeoutMs;
  for (;;) {
    const el = findTarget(name);
    if (el && (!ready || ready(el))) return el;
    if (Date.now() >= deadline) return el;
    await sleep(150);
  }
}

/** 同时等多个锚点，先出现者胜——click+skipIf 用：谁先挂上就说明页面在
 *  哪个状态（列表页有 kb-card、详情页有 upload-zone），不用为必然缺席的
 *  那个白等 4 秒。 */
async function waitForAny(names: string[], timeoutMs = 4000): Promise<string | null> {
  const deadline = Date.now() + timeoutMs;
  for (;;) {
    for (const n of names) if (findTarget(n)) return n;
    if (Date.now() >= deadline) return null;
    await sleep(150);
  }
}

function highlight(el: HTMLElement) {
  el.classList.add("kbase-tour-target");
  highlighted.add(el);
}

/** 目标被卡片挡住时把卡片挪到另半屏（只挪不落盘——用户手拖的才记住）。 */
function dodge(el: HTMLElement) {
  const card = cardEl.value;
  if (!card) return;
  const tr = el.getBoundingClientRect();
  const cr = card.getBoundingClientRect();
  const overlap = tr.left < cr.right && tr.right > cr.left
    && tr.top < cr.bottom && tr.bottom > cr.top;
  if (!overlap) return;
  const y = tr.top + tr.height / 2 > window.innerHeight / 2
    ? 16 : window.innerHeight - cr.height - 16;
  pos.value = clampPos({ x: cr.left, y }, cr.width, cr.height,
    window.innerWidth, window.innerHeight);
}

/** 逐字填入（演示感）；只认原生 input/textarea。用户已自己输入不同内容时
 *  不覆盖（重放盖掉自己之前填的示例无妨）。设 value 后派发 input 事件驱动
 *  v-model。 */
async function typeInto(el: HTMLElement, text: string, seq: number) {
  if (!(el instanceof HTMLTextAreaElement || el instanceof HTMLInputElement)) return;
  if (el.disabled || (el.value && !text.startsWith(el.value))) return;
  el.focus();
  for (let i = 1; i <= text.length; i += 1) {
    if (seq !== runSeq) return;
    el.value = text.slice(0, i);
    el.dispatchEvent(new Event("input", { bubbles: true }));
    await sleep(18);
  }
}

function resolveText(op: Extract<DemoOp, { do: "type" }>): string {
  if (op.textFrom) {
    const text = findTarget(op.textFrom)?.textContent?.trim();
    if (text) return text;
  }
  return t(op.textKey);
}

async function runDemo() {
  clearHighlights();               // 同时使旧一轮失效（runSeq++）
  const seq = runSeq;
  if (!props.enabled) return;      // 非白名单账号绝不替人操作页面
  const ops = cur.value.demo;
  if (!ops?.length) return;
  await nextTick();
  for (const op of ops) {
    if (op.do === "click") {
      // skipIf 与 target 竞速：先出现者定夺（见 waitForAny 注释）
      if (op.skipIf) {
        const winner = await waitForAny([op.skipIf, op.target]);
        if (seq !== runSeq) return;
        if (winner === op.skipIf || winner === null) continue;
      }
      const el = await waitFor(op.target);
      if (seq !== runSeq) return;
      if (!el) continue;
      // 已展开的弹层触发器不再点（重放时避免把引用预览又点关了）
      if (el.getAttribute("aria-expanded") === "true") continue;
      el.scrollIntoView({ block: "center", behavior: "smooth" });
      el.click();
      await sleep(300);            // 给点击引发的路由切换/弹层一点时间
      continue;
    }
    // type 要等到输入框可用（如问答页 kbId 就位前 textarea 是 disabled）
    const el = await waitFor(op.target, 4000, op.do === "type"
      ? (e) => !(e as HTMLInputElement).disabled : undefined);
    if (seq !== runSeq) return;
    if (!el) continue;             // 锚点缺席（页面状态没到）：跳过，提示框兜底
    el.scrollIntoView({ block: "center", behavior: "smooth" });
    await sleep(250);              // 等平滑滚动大致就位再量避让
    if (seq !== runSeq) return;
    dodge(el);
    highlight(el);
    if (op.do === "focus") el.focus();
    if (op.do === "type") await typeInto(el, resolveText(op), seq);
    await sleep(350);              // 多目标依次呈现，不一拥而上
  }
}

// ---- 卡片拖拽（按住顶部色条移动；位置跨步骤/跨 SPA 记忆） ----

const cardEl = ref<HTMLElement | null>(null);
const pos = ref<CardPos | null>(loadCardPos());
const dragging = ref(false);
let dragOffset = { x: 0, y: 0 };

// 卡片一挂上就把记忆位置夹回当前视口——大屏拖到右下角、小屏（或跨端后
// 窗口变小）打开时，不夹取的话整张卡在屏外够不着
watch(cardEl, (el) => {
  if (!el || !pos.value) return;
  const r = el.getBoundingClientRect();
  pos.value = clampPos(pos.value, r.width, r.height, window.innerWidth, window.innerHeight);
});

function onDragStart(e: PointerEvent) {
  // 色条上的最小化/退出按钮不触发拖拽
  if ((e.target as HTMLElement).closest("button")) return;
  const el = cardEl.value;
  if (!el) return;
  const r = el.getBoundingClientRect();
  dragOffset = { x: e.clientX - r.left, y: e.clientY - r.top };
  dragging.value = true;
  window.addEventListener("pointermove", onDragMove);
  window.addEventListener("pointerup", onDragEnd, { once: true });
  window.addEventListener("pointercancel", onDragEnd, { once: true });
  e.preventDefault();              // 防止拖动中选中文本
}

function onDragMove(e: PointerEvent) {
  const el = cardEl.value;
  if (!dragging.value || !el) return;
  const r = el.getBoundingClientRect();
  pos.value = clampPos(
    { x: e.clientX - dragOffset.x, y: e.clientY - dragOffset.y },
    r.width, r.height, window.innerWidth, window.innerHeight);
}

function onDragEnd() {
  dragging.value = false;
  window.removeEventListener("pointermove", onDragMove);
  window.removeEventListener("pointerup", onDragEnd);
  window.removeEventListener("pointercancel", onDragEnd);
  if (pos.value) saveCardPos(pos.value);
}

/** 窗口缩小后把记忆位置夹回视口，避免卡片飞出屏幕外够不着。 */
function onResize() {
  const el = cardEl.value;
  if (!pos.value || !el) return;
  const r = el.getBoundingClientRect();
  pos.value = clampPos(pos.value, r.width, r.height, window.innerWidth, window.innerHeight);
}
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

    <!-- 导览卡：默认底部居中；拖过之后按记忆位置绝对定位。无遮罩，页面可继续操作 -->
    <div
      v-else
      ref="cardEl"
      class="fixed z-50 w-[400px] max-w-[calc(100vw-24px)] overflow-hidden rounded-xl border border-[var(--border)] bg-[var(--surface)] shadow-[var(--shadow-drawer)]"
      :class="pos ? '' : 'bottom-5 left-1/2 -translate-x-1/2'"
      :style="pos ? { left: `${pos.x}px`, top: `${pos.y}px` } : undefined"
    >
      <div
        class="flex touch-none select-none items-center justify-between bg-[var(--accent)] px-3.5 py-2"
        :class="dragging ? 'cursor-grabbing' : 'cursor-grab'"
        :title="t('tour.ui.drag')"
        @pointerdown="onDragStart"
      >
        <span class="flex items-center gap-1.5 text-xs font-semibold text-white">
          <GripVertical class="size-3.5 text-white/60" />
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
            :title="t('tour.ui.exit')" @click="exitTour"
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
        <!-- 任何带演示动作的步骤都可重放（不只 action 步） -->
        <button
          v-if="cur.demo"
          type="button"
          class="flex items-center gap-1 rounded px-1 py-0.5 text-[11px] font-medium text-[var(--accent-text)] transition-colors hover:bg-[var(--accent-weak)]"
          @click="runDemo()"
        >
          <RotateCcw class="size-3" /> {{ t("tour.ui.replay") }}
        </button>
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

<style>
/* 演示动作高亮圈（全局：目标元素散布在各视图里）。outline 是主视觉，
   box-shadow 脉冲是氛围——不支持的浏览器只少个呼吸感，不影响指认。 */
.kbase-tour-target {
  outline: 2px solid var(--accent) !important;
  outline-offset: 2px;
  border-radius: 6px;
  animation: kbase-tour-pulse 1.5s ease-in-out infinite;
}
@keyframes kbase-tour-pulse {
  0%, 100% { box-shadow: 0 0 0 3px var(--accent-weak); }
  50% { box-shadow: 0 0 0 9px var(--accent-weak); }
}
</style>
