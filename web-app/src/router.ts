import { createRouter, createWebHistory } from "vue-router";
import ChatView from "@/views/ChatView.vue";
import KbView from "@/views/KbView.vue";
import AnalysisView from "@/views/AnalysisView.vue";
import GenerateView from "@/views/GenerateView.vue";
import SettingsView from "@/views/SettingsView.vue";
import LoginView from "@/views/LoginView.vue";
import { getSession, setUnauthorizedHandler } from "@/lib/api";
import { loginRedirectQuery } from "@/lib/auth-utils";

const router = createRouter({
  history: createWebHistory(),
  routes: [
    { path: "/login", name: "login", component: LoginView },
    { path: "/", name: "chat", component: ChatView },
    { path: "/kb", name: "kb", component: KbView },
    { path: "/analysis", name: "analysis", component: AnalysisView },
    { path: "/generate", name: "generate", component: GenerateView },
    { path: "/settings", name: "settings", component: SettingsView },
  ],
});

// 全局会话守卫：/login 本身开放，其余路由都探测会话（getSession 内部缓存，
// 不会每次导航都打请求）。无会话则跳 /login 并带上原目标路径，登录后
// LoginView 用 redirectTarget 读回来跳回去。
router.beforeEach(async (to) => {
  if (to.path === "/login") return true;
  const session = await getSession();
  if (session) return true;
  return { path: "/login", query: loginRedirectQuery(to.fullPath) };
});

// 401 拦截钩子在此注册（而不是 api.ts 直接 import router）：api.ts 是被
// router.ts 引入的底层模块，反过来 import router 会成环；用回调注册的方式
// 让依赖方向保持单向 api.ts ← router.ts。跳转用 router.push 而非
// window.location，避免整页刷新丢失 SPA 状态。
setUnauthorizedHandler(() => {
  const current = router.currentRoute.value;
  if (current.path === "/login") return; // 已在登录页，不重复跳转造成循环
  router.push({ path: "/login", query: loginRedirectQuery(current.fullPath) });
});

export default router;
