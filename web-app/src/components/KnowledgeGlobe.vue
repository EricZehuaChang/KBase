<script setup lang="ts">
// 3D「知识星球」背景（手写 canvas，零依赖）：费氏球面均匀布点 + 近邻连线
// 构成一颗缓慢自转的知识网络球，鼠标移动带轻微视差。accent 色低透明度，
// 是氛围层不是内容层——铺在容器底层，不拦截指针事件。
//
// 响应体验设计：
// - DPR 感知（高分屏不糊）+ ResizeObserver 跟随容器尺寸；
// - prefers-reduced-motion：只画静止一帧，不进动画循环；
// - 标签页隐藏时 requestAnimationFrame 天然暂停，零后台开销；
// - 连线拓扑按球面角距离预计算一次（自转不改变点间距离），逐帧只做旋转投影。
//
// 若后续要上 Spline 设计场景：导出 .splinecode 自托管 + @splinetool/runtime，
// 替换本组件内部实现即可——对外契约只是"铺满容器的背景块"。
import { onBeforeUnmount, onMounted, ref } from "vue";

const props = withDefaults(defineProps<{
  /** 点数密度系数（1=约 110 点） */ density?: number;
  /** 整体透明度 */ opacity?: number;
}>(), { density: 1, opacity: 1 });

const canvasEl = ref<HTMLCanvasElement | null>(null);
let cleanup: (() => void) | null = null;

onMounted(() => {
  const canvas = canvasEl.value;
  const ctx = canvas?.getContext("2d");
  if (!canvas || !ctx) return;      // jsdom/极端环境：无 canvas 就整体不画

  const N = Math.round(110 * props.density);
  // 费氏球面均匀布点（黄金角螺旋），单位球坐标
  const pts: [number, number, number][] = [];
  const golden = Math.PI * (3 - Math.sqrt(5));
  for (let i = 0; i < N; i++) {
    const y = 1 - (i / (N - 1)) * 2;
    const r = Math.sqrt(1 - y * y);
    const a = golden * i;
    pts.push([Math.cos(a) * r, y, Math.sin(a) * r]);
  }
  // 近邻连线拓扑预计算（角距离阈值随密度自适应，自转不变量）
  const edges: [number, number][] = [];
  const maxD = 0.42 / Math.sqrt(props.density);
  for (let i = 0; i < N; i++) {
    for (let j = i + 1; j < N; j++) {
      const dx = pts[i][0] - pts[j][0];
      const dy = pts[i][1] - pts[j][1];
      const dz = pts[i][2] - pts[j][2];
      if (dx * dx + dy * dy + dz * dz < maxD * maxD) edges.push([i, j]);
    }
  }

  let w = 0; let h = 0; let accent = "#534AB7";
  const dpr = Math.min(window.devicePixelRatio || 1, 2);
  function resize() {
    const rect = canvas!.parentElement?.getBoundingClientRect();
    if (!rect) return;
    w = rect.width; h = rect.height;
    canvas!.width = Math.round(w * dpr);
    canvas!.height = Math.round(h * dpr);
    ctx!.setTransform(dpr, 0, 0, dpr, 0, 0);
    accent = getComputedStyle(document.documentElement)
      .getPropertyValue("--accent").trim() || accent;
  }
  resize();
  const ro = new ResizeObserver(resize);
  if (canvas.parentElement) ro.observe(canvas.parentElement);

  // 鼠标视差（目标值 + 每帧缓动，跟手但不生硬）
  let targetTiltX = 0; let targetTiltY = 0; let tiltX = 0; let tiltY = 0;
  function onPointer(e: PointerEvent) {
    targetTiltY = (e.clientX / window.innerWidth - 0.5) * 0.5;
    targetTiltX = (e.clientY / window.innerHeight - 0.5) * 0.35;
  }
  window.addEventListener("pointermove", onPointer);

  let rot = 0; let raf = 0;
  function frame() {
    rot += 0.0016;
    tiltX += (targetTiltX - tiltX) * 0.04;
    tiltY += (targetTiltY - tiltY) * 0.04;
    draw();
    raf = requestAnimationFrame(frame);
  }

  function draw() {
    ctx!.clearRect(0, 0, w, h);
    const R = Math.min(w, h) * 0.36;
    const cx = w / 2; const cy = h / 2;
    const persp = 3.2;               // 透视强度：z 越远越小越淡
    const cosR = Math.cos(rot + tiltY); const sinR = Math.sin(rot + tiltY);
    const cosT = Math.cos(tiltX); const sinT = Math.sin(tiltX);
    // 旋转（Y 轴自转 + X 轴视差倾斜）后透视投影
    const proj: [number, number, number][] = pts.map(([x, y, z]) => {
      const x1 = x * cosR + z * sinR;
      const z1 = -x * sinR + z * cosR;
      const y1 = y * cosT - z1 * sinT;
      const z2 = y * sinT + z1 * cosT;
      const s = persp / (persp - z2);
      return [cx + x1 * R * s, cy + y1 * R * s, z2];
    });
    ctx!.lineWidth = 1;
    for (const [i, j] of edges) {
      const za = (proj[i][2] + proj[j][2]) / 2;      // 深度均值定连线浓淡
      ctx!.globalAlpha = props.opacity * (0.05 + 0.13 * (za + 1) / 2);
      ctx!.strokeStyle = accent;
      ctx!.beginPath();
      ctx!.moveTo(proj[i][0], proj[i][1]);
      ctx!.lineTo(proj[j][0], proj[j][1]);
      ctx!.stroke();
    }
    for (const [x, y, z] of proj) {
      const depth = (z + 1) / 2;
      ctx!.globalAlpha = props.opacity * (0.25 + 0.55 * depth);
      ctx!.fillStyle = accent;
      ctx!.beginPath();
      ctx!.arc(x, y, 1.1 + 1.7 * depth, 0, Math.PI * 2);
      ctx!.fill();
    }
    ctx!.globalAlpha = 1;
  }

  const reduced = window.matchMedia("(prefers-reduced-motion: reduce)").matches;
  if (reduced) draw();               // 动效敏感用户：静止一帧，仍有画面
  else raf = requestAnimationFrame(frame);

  cleanup = () => {
    cancelAnimationFrame(raf);
    ro.disconnect();
    window.removeEventListener("pointermove", onPointer);
  };
});

onBeforeUnmount(() => cleanup?.());
</script>

<template>
  <div class="pointer-events-none absolute inset-0 overflow-hidden" aria-hidden="true">
    <canvas ref="canvasEl" class="block h-full w-full" />
  </div>
</template>
