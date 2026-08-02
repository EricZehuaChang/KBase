<script setup lang="ts">
// 登录页特性轮播（Swiper 交互习惯，手写零依赖）：自动播放 + 圆点导航 +
// 指尖/鼠标滑动切换 + 悬停暂停。三张卡文案走 i18n（login.slideN_*），
// prefers-reduced-motion 用户不自动播放（仍可点圆点/滑动手动切）。
import { computed, onBeforeUnmount, onMounted, ref } from "vue";
import { useI18n } from "vue-i18n";
import { FileSearch, Share2, ShieldCheck } from "@lucide/vue";

const { t } = useI18n();

const SLIDES = [
  { key: 1, icon: FileSearch },
  { key: 2, icon: Share2 },
  { key: 3, icon: ShieldCheck },
] as const;

const cur = ref(0);
const slide = computed(() => SLIDES[cur.value]);

let timer: ReturnType<typeof setInterval> | null = null;
const reduced = typeof window !== "undefined"
  && window.matchMedia("(prefers-reduced-motion: reduce)").matches;

function go(i: number) {
  cur.value = (i + SLIDES.length) % SLIDES.length;
}
function play() {
  if (reduced || timer) return;
  timer = setInterval(() => go(cur.value + 1), 4500);
}
function pause() {
  if (timer) { clearInterval(timer); timer = null; }
}
onMounted(play);
onBeforeUnmount(pause);

// 指尖滑动：按下记 x，抬起位移超阈值翻页（期间暂停自动播放）
let downX: number | null = null;
function onDown(e: PointerEvent) { downX = e.clientX; pause(); }
function onUp(e: PointerEvent) {
  if (downX !== null) {
    const dx = e.clientX - downX;
    if (Math.abs(dx) > 40) go(cur.value + (dx < 0 ? 1 : -1));
  }
  downX = null;
  play();
}
</script>

<template>
  <div
    class="select-none"
    @pointerdown="onDown"
    @pointerup="onUp"
    @pointerleave="downX = null"
    @mouseenter="pause"
    @mouseleave="play"
  >
    <Transition name="carousel" mode="out-in">
      <div :key="slide.key" class="flex min-h-24 flex-col gap-1.5">
        <component :is="slide.icon" class="size-6 text-[var(--accent)]" />
        <h3 class="text-[15px] font-semibold">{{ t(`login.slide${slide.key}_title`) }}</h3>
        <p class="text-sm leading-relaxed text-[var(--text-2)]">
          {{ t(`login.slide${slide.key}_desc`) }}
        </p>
      </div>
    </Transition>
    <div class="mt-4 flex gap-1.5">
      <button
        v-for="(s, i) in SLIDES"
        :key="s.key"
        type="button"
        class="h-1.5 rounded-full transition-all"
        :class="i === cur ? 'w-5 bg-[var(--accent)]' : 'w-1.5 bg-[var(--border-strong)] hover:bg-[var(--text-3)]'"
        :aria-label="t(`login.slide${s.key}_title`)"
        @click="go(i)"
      />
    </div>
  </div>
</template>

<style scoped>
.carousel-enter-active,
.carousel-leave-active {
  transition: opacity 0.3s ease, transform 0.3s ease;
}
.carousel-enter-from {
  opacity: 0;
  transform: translateY(8px);
}
.carousel-leave-to {
  opacity: 0;
  transform: translateY(-8px);
}
@media (prefers-reduced-motion: reduce) {
  .carousel-enter-active,
  .carousel-leave-active {
    transition: none;
  }
}
</style>
