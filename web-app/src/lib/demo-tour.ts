// 产品导览（对标 lotle TourGuide 模式）：步骤数组驱动的"下一步"式功能巡礼，
// 录屏/伙伴自学两用。与 lotle 单 SPA 不同，KBase 是 portal+admin 双 SPA——
// 跨端步骤走整页跳转，状态存 localStorage，落地端的 DemoTour 实例自动续步。
// 文案全部走 i18n（tour.* 三语），录屏用英文界面时卡片即英文提词器。
//
// demo 演示动作：到达步骤后由 DemoTour 自动执行的页面联动（找 [data-tour=xx]
// 锚点），让导览"替用户操作"而不只是文字提示——填示例问题、点进知识库、
// 点开引用角标、高亮/聚焦目标按钮。锚点可能随页面状态缺席（如还没有回答
// 就没有角标），运行器找不到目标时静默跳过该原语，提示框文案仍在兜底。
import { ref } from "vue";

/** 演示原语（按序执行，目标=页面元素的 data-tour 属性值）： */
export type DemoOp =
  /** 滚动到目标并加脉冲高亮圈 */
  | { do: "highlight"; target: string }
  /** 高亮 + focus——"选中"按钮/下拉触发器，用户按 Enter 即可展开 */
  | { do: "focus"; target: string }
  /** 替用户点击；skipIf 锚点已在场则跳过（如已在库详情页就不再点卡片） */
  | { do: "click"; target: string; skipIf?: string }
  /** 往输入框逐字填入内容：优先取 textFrom 锚点的文本（如空态快捷问题——
   *  按当前库真实文档生成、检索必命中），否则用 textKey 的 i18n 文案 */
  | { do: "type"; target: string; textKey: string; textFrom?: string };

export interface TourStep {
  app: "portal" | "admin";
  /** 该步应处的路由（可含 ?tab= 查询串；比较时只看 pathname） */
  path: string;
  /** i18n 前缀：tour.<key>.title / .desc /（可选）.action */
  key: string;
  /** 有"请操作页面"提示框（文案在 tour.<key>.action） */
  action?: boolean;
  /** 到达该步后自动执行的演示动作（可用卡片上的"再演示一次"重放） */
  demo?: DemoOp[];
}

// 顺序即导览顺序：使用端问答 → 工作台数据侧 → 质量 → 生成 → 治理 → 收尾
export const TOUR_STEPS: TourStep[] = [
  { app: "portal", path: "/", key: "welcome" },
  { app: "portal", path: "/", key: "ask", action: true, demo: [
    { do: "highlight", target: "kb-select" },
    { do: "type", target: "chat-input", textKey: "tour.ask.sample", textFrom: "empty-question" },
    { do: "highlight", target: "send" },
  ] },
  { app: "portal", path: "/", key: "citations", action: true, demo: [
    { do: "click", target: "citation-marker" },
  ] },
  { app: "portal", path: "/", key: "models", action: true, demo: [
    { do: "focus", target: "model-select" },
    { do: "highlight", target: "joint-search" },
  ] },
  { app: "portal", path: "/", key: "sessions", demo: [
    // 窄屏/手动收起时侧栏是折叠态，先替用户展开再高亮
    { do: "click", target: "session-expand", skipIf: "session-sidebar" },
    { do: "highlight", target: "session-sidebar" },
  ] },
  { app: "admin", path: "/", key: "workbench", demo: [
    // 从库详情返回列表（若在），再高亮库卡片
    { do: "click", target: "kb-back", skipIf: "kb-card" },
    { do: "highlight", target: "kb-card" },
  ] },
  { app: "admin", path: "/", key: "upload", action: true, demo: [
    // 替用户点进第一个知识库（已在详情页则不动），高亮上传虚线框
    { do: "click", target: "kb-card", skipIf: "upload-zone" },
    { do: "highlight", target: "upload-zone" },
  ] },
  { app: "admin", path: "/", key: "sync_share", demo: [
    { do: "click", target: "kb-card", skipIf: "kb-actions" },
    { do: "highlight", target: "kb-actions" },
  ] },
  { app: "admin", path: "/analysis", key: "analysis", demo: [
    { do: "type", target: "analysis-query", textKey: "tour.analysis.sample" },
    { do: "highlight", target: "analysis-run" },
  ] },
  { app: "admin", path: "/generate", key: "generate", demo: [
    { do: "highlight", target: "generate-panel" },
  ] },
  { app: "admin", path: "/settings?tab=providers", key: "providers", demo: [
    { do: "focus", target: "add-provider" },
  ] },
  { app: "admin", path: "/settings?tab=access", key: "users", demo: [
    { do: "highlight", target: "settings-access" },
  ] },
  { app: "admin", path: "/translations", key: "translations", demo: [
    { do: "type", target: "translations-search", textKey: "tour.translations.sample" },
  ] },
  { app: "admin", path: "/settings?tab=ops", key: "ops", demo: [
    { do: "highlight", target: "settings-ops" },
  ] },
  { app: "portal", path: "/", key: "finish" },
];

const STORAGE_KEY = "kbase-tour";
const POS_KEY = "kbase-tour-pos";

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

// ---- 导览卡拖拽位置（跨步骤/跨 SPA 记忆；null=默认底部居中） ----

export interface CardPos { x: number; y: number }

/** 把卡片位置夹回视口内（8px 边距；卡片比视口还大时贴边保住左上角）。 */
export function clampPos(
  pos: CardPos, w: number, h: number, vw: number, vh: number,
): CardPos {
  return {
    x: Math.max(8, Math.min(pos.x, vw - w - 8)),
    y: Math.max(8, Math.min(pos.y, vh - h - 8)),
  };
}

export function saveCardPos(pos: CardPos): void {
  localStorage.setItem(POS_KEY, JSON.stringify(pos));
}

export function loadCardPos(): CardPos | null {
  try {
    const raw = localStorage.getItem(POS_KEY);
    if (!raw) return null;
    const p = JSON.parse(raw) as CardPos;
    if (typeof p.x !== "number" || typeof p.y !== "number") return null;
    return p;
  } catch {
    return null;
  }
}
