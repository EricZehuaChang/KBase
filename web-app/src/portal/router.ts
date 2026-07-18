// 【使用端】路由表（Vite 入口 index.html，base "/"）。只有两个路由——问答
// 首页与登录页；KbView/AnalysisView/GenerateView/SettingsView 完全不在这里
// 出现，这正是"使用端 bundle 不含管理端代码"的路由侧前提（配合
// web-app/scripts/check-bundle-isolation.mjs 的构建产物校验，见 spec §3.3）。
// ChatHome（M5-1 F2 从 src/views/ChatView.vue 移入并重构）现在物理上就在
// src/portal/ 目录下，不再是"共享视图"——它只属于使用端。
import { createRouter, createWebHistory } from "vue-router";
import ChatHome from "./ChatHome.vue";
import LoginView from "@/views/LoginView.vue";
import { getSession, setUnauthorizedHandler } from "@/lib/api";
import { loginRedirectQuery } from "@/lib/auth-utils";

const router = createRouter({
  history: createWebHistory("/"),
  routes: [
    { path: "/login", name: "portal-login", component: LoginView },
    // 免登录分享页：token 即授权，不走会话守卫（对标 Dify WebApp 形态）
    { path: "/share/:token", name: "portal-share",
      component: () => import("@/views/ShareView.vue") },
    { path: "/", name: "portal-chat", component: ChatHome },
  ],
});

// 会话守卫：逻辑与分端改造前一致（未登录跳 /login 并带 redirect）。使用端
// 不做角色校验——viewer/editor/admin 登录后都落地问答页，角色只影响顶栏
// "进入工作台"入口是否可见（PortalShell.vue），不影响这两个路由本身能否
// 进入。角色相关的强制拦截只存在于管理端（src/admin/guard.ts）。
router.beforeEach(async (to) => {
  if (to.path === "/login" || to.path.startsWith("/share/")) return true;
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
  // 登录页不重复跳转；/share/ 免登录页对 401 免疫（匿名访客本就无会话，
  // 跳登录等于把分享链接变成登录墙）
  if (current.path === "/login" || current.path.startsWith("/share/")) return;
  router.push({ path: "/login", query: loginRedirectQuery(current.fullPath) });
});

export default router;
