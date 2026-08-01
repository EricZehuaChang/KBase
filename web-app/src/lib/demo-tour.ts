// 产品导览（对标 lotle TourGuide 模式）：步骤数组驱动的"下一步"式功能巡礼，
// 录屏/伙伴自学两用。与 lotle 单 SPA 不同，KBase 是 portal+admin 双 SPA——
// 跨端步骤走整页跳转，状态存 localStorage，落地端的 DemoTour 实例自动续步。
// 文案全部走 i18n（tour.* 三语），录屏用英文界面时卡片即英文提词器。
import { ref } from "vue";

export interface TourStep {
  app: "portal" | "admin";
  /** 该步应处的路由（可含 ?tab= 查询串；比较时只看 pathname） */
  path: string;
  /** i18n 前缀：tour.<key>.title / .desc /（可选）.action */
  key: string;
  /** 有"请操作页面"提示框（文案在 tour.<key>.action） */
  action?: boolean;
}

// 顺序即导览顺序：使用端问答 → 工作台数据侧 → 质量 → 生成 → 治理 → 收尾
export const TOUR_STEPS: TourStep[] = [
  { app: "portal", path: "/", key: "welcome" },
  { app: "portal", path: "/", key: "ask", action: true },
  { app: "portal", path: "/", key: "citations", action: true },
  { app: "portal", path: "/", key: "models", action: true },
  { app: "portal", path: "/", key: "sessions" },
  { app: "admin", path: "/", key: "workbench" },
  { app: "admin", path: "/", key: "upload", action: true },
  { app: "admin", path: "/", key: "sync_share" },
  { app: "admin", path: "/analysis", key: "analysis" },
  { app: "admin", path: "/generate", key: "generate" },
  { app: "admin", path: "/settings?tab=providers", key: "providers" },
  { app: "admin", path: "/settings?tab=access", key: "users" },
  { app: "admin", path: "/translations", key: "translations" },
  { app: "admin", path: "/settings?tab=ops", key: "ops" },
  { app: "portal", path: "/", key: "finish" },
];

const STORAGE_KEY = "kbase-tour";

// 模块级响应式状态：UserMenu（启动入口）与 DemoTour（渲染）共享
export const tourActive = ref(false);
export const tourStep = ref(0);

export function saveTourState(): void {
  localStorage.setItem(STORAGE_KEY, JSON.stringify(
    { active: tourActive.value, step: tourStep.value }));
}

/** 应用启动时恢复（跨 SPA 跳转后由落地端调用）。 */
export function loadTourState(): void {
  try {
    const raw = localStorage.getItem(STORAGE_KEY);
    if (!raw) return;
    const s = JSON.parse(raw) as { active?: boolean; step?: number };
    if (s.active) {
      tourActive.value = true;
      tourStep.value = Math.min(Math.max(0, s.step ?? 0), TOUR_STEPS.length - 1);
    }
  } catch { /* 脏状态当作未开启 */ }
}

export function startTour(): void {
  tourStep.value = 0;
  tourActive.value = true;
  saveTourState();
}

export function stopTour(): void {
  tourActive.value = false;
  localStorage.removeItem(STORAGE_KEY);
}

/** 步骤路径 → 浏览器地址（跨 SPA 整页跳转用）：admin SPA 挂在 /admin 前缀。 */
export function stepHref(step: TourStep): string {
  return step.app === "admin" ? `/admin${step.path}` : step.path;
}

/** 当前路由是否已在该步位置（只比 pathname，忽略 ?tab= 查询串）。 */
export function samePath(routePath: string, step: TourStep): boolean {
  return routePath === step.path.split("?")[0];
}
