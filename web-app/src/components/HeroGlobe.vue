<script setup lang="ts">
// 登录页 hero 3D 地球：cobe（GitHub 首页地球同款风格的 WebGL 实现，
// shuding/cobe，MIT，~5KB，随包打包离线可用）。生产级观感来自它的
// 着色点阵球 + 辉光，比手写 canvas 网络球高一档——手写版（KnowledgeGlobe）
// 留给问答空态做低调氛围层，两者分工不同。
//
// 交互与响应体验：
// - 自转 + 指尖拖拽旋转（拖动时暂停自转，松手回落自转，惯性由缓动提供）；
// - prefers-reduced-motion：不自转，仍可手动拖；
// - ResizeObserver 跟随容器；组件卸载 destroy() 释放 WebGL 上下文；
// - 标记点讲业务故事：吉隆坡/新加坡/槟城/北京/上海（马来西亚+东南亚+中国）。
import { onBeforeUnmount, onMounted, ref } from "vue";
import createGlobe, { type Globe } from "cobe";

const canvasEl = ref<HTMLCanvasElement | null>(null);
let globe: Globe | null = null;
let cleanup: (() => void) | null = null;

onMounted(() => {
  const canvas = canvasEl.value;
  if (!canvas) return;
  const reduced = window.matchMedia("(prefers-reduced-motion: reduce)").matches;

  let phi = 0.9;                    // 初始转角：东南亚朝前
  let vel = 0;                      // 拖拽速度（松手后缓动衰减回自转）
  let dragging: number | null = null;
  let w = 0;

  const setSize = () => {
    w = canvas.offsetWidth;
  };
  setSize();
  const ro = new ResizeObserver(setSize);
  ro.observe(canvas);

  try {
    globe = createGlobe(canvas, {
      devicePixelRatio: 2,
      // cobe v2 内部会按 devicePixelRatio 放大缓冲，这里传 CSS 尺寸即可
      width: w,
      height: w,
      phi,
      theta: 0.22,
      dark: 1,
      diffuse: 1.6,
      mapSamples: 18000,
      mapBrightness: 5.4,
      baseColor: [0.32, 0.32, 0.42],
      markerColor: [0.62, 0.58, 1],
      glowColor: [0.28, 0.26, 0.6],
      markers: [
        { location: [3.14, 101.69], size: 0.08 },   // 吉隆坡
        { location: [1.35, 103.82], size: 0.06 },   // 新加坡
        { location: [5.41, 100.33], size: 0.05 },   // 槟城
        { location: [39.9, 116.4], size: 0.06 },    // 北京
        { location: [31.23, 121.47], size: 0.06 },  // 上海
      ],
    });
  } catch {
    return;                          // WebGL 不可用（远程桌面/老设备）：留空不阻塞登录
  }

  // cobe v2 无 onRender：渲染由 update() 驱动，自己开 rAF 循环。
  // phi/尺寸没变化时跳过 update（reduced-motion 静止态零开销）。
  let raf = 0;
  let lastPhi = Number.NaN;
  let lastW = 0;
  const loop = () => {
    if (dragging === null) {
      if (!reduced) phi += 0.0028;    // 自转（动效敏感用户静止，可手动拖）
      phi += vel;
      vel *= 0.93;                    // 松手惯性衰减
      if (Math.abs(vel) < 0.00003) vel = 0;
    }
    if (phi !== lastPhi || w !== lastW) {
      lastPhi = phi; lastW = w;
      globe!.update({ phi, width: w, height: w });
    }
    raf = requestAnimationFrame(loop);
  };
  raf = requestAnimationFrame(loop);

  // 指尖拖拽旋转（pointer 事件统一鼠标/触屏）
  let lastX = 0;
  const down = (e: PointerEvent) => {
    dragging = e.pointerId; lastX = e.clientX;
    canvas.style.cursor = "grabbing";
  };
  const move = (e: PointerEvent) => {
    if (dragging !== e.pointerId) return;
    phi += (e.clientX - lastX) / 180;
    vel = (e.clientX - lastX) / 600;
    lastX = e.clientX;
  };
  const up = () => {
    dragging = null;
    canvas.style.cursor = "grab";
  };
  canvas.addEventListener("pointerdown", down);
  window.addEventListener("pointermove", move);
  window.addEventListener("pointerup", up);
  window.addEventListener("pointercancel", up);

  cleanup = () => {
    cancelAnimationFrame(raf);
    ro.disconnect();
    canvas.removeEventListener("pointerdown", down);
    window.removeEventListener("pointermove", move);
    window.removeEventListener("pointerup", up);
    window.removeEventListener("pointercancel", up);
  };
});

onBeforeUnmount(() => {
  cleanup?.();
  globe?.destroy();
});
</script>

<template>
  <!-- 正方形画布由外层容器定宽高；渐显避免 WebGL 首帧闪黑 -->
  <canvas
    ref="canvasEl"
    class="hero-globe block h-full w-full cursor-grab touch-none"
    aria-hidden="true"
  />
</template>

<style scoped>
.hero-globe {
  animation: hero-globe-in 1.2s ease-out both;
}
@keyframes hero-globe-in {
  from { opacity: 0; }
  to { opacity: 1; }
}
@media (prefers-reduced-motion: reduce) {
  .hero-globe {
    animation: none;
  }
}
</style>
