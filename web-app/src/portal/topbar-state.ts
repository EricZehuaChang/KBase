// 使用端顶栏选择器（KB/模型下拉）共享状态（M5-1 F2）。
//
// 背景：F1 时这两个下拉长在 ChatView 自己的 <header> 里；F2 把它们搬进
// PortalShell 的顶栏——顶栏跨路由常驻（不随问答页切换/卸载），而实际消费
// 选中值发起会话/问答请求的是 ChatHome（PortalShell 的 router-view 子级）。
// 两者不在同一条 props 传递链路上（PortalShell 用 <router-view> 渲染
// ChatHome，不是直接的父子模板嵌套），所以用模块级单例 ref 共享——与
// src/lib/api.ts 的 currentRole、src/lib/theme.ts 的 theme 是同一手法，
// 生命周期就是"当前登录会话"，没必要为这两个值引入 Pinia。
import { ref, watch } from "vue";
import { listKbs, listProviders, type Kb } from "@/lib/api";

export const kbs = ref<Kb[]>([]);
export const kbId = ref<string | undefined>(undefined);
export const providers = ref<string[]>([]);
export const provider = ref<string | undefined>(undefined);

// M6-2 多库联合问答：主库（kbId）之外额外联查的库。只影响"新建会话"——
// 会话建立后 kb_ids 固定在服务端，中途改选不影响已开会话。主库切换时清空：
// 联查组合是围绕主库挑的，换了主库原组合大概率失义，静默保留容易误导。
export const extraKbIds = ref<string[]>([]);
watch(kbId, () => { extraKbIds.value = []; });

let loaded = false;

/** 幂等加载：PortalShell 挂载时调用。kbs/providers 在一次登录会话内基本
 * 不变，不需要每次路由切换/组件重挂载都重新拉取——用模块级标志位而不是
 * 组件内 onMounted 判空，避免"用户在两次挂载之间手动删了所有 KB"这类
 * 极端场景下的重复请求成本（这个成本本来就小，标志位只是不做无谓请求）。 */
export async function ensureTopbarLoaded(): Promise<void> {
  if (loaded) return;
  loaded = true;
  try {
    kbs.value = await listKbs();
    if (kbs.value.length && !kbId.value) kbId.value = kbs.value[0].id;
    const providersResp = await listProviders();
    providers.value = providersResp.providers;
    if (!provider.value) provider.value = providersResp.active ?? undefined;
  } catch (err) {
    // 失败必须回滚闩锁：应用启动瞬间路由还是占位 "/"，PortalShell 的
    // watch 会在未登录时提前触发一次本函数（401）——若闩锁不回滚，登录
    // 成功后的再次调用会被永久短路，首次登录的用户面对空的知识库下拉，
    // 只能刷新页面自救（真机 Playwright 全新会话实测踩中）。
    loaded = false;
    console.warn("顶栏 KB/模型加载失败（将在下次路由变化时重试）:", err);
  }
}
