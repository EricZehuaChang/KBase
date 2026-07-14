// 【管理端】路由表（Vite 入口 admin.html，base "/admin"）。
import { createRouter, createWebHistory } from "vue-router";
import LoginView from "@/views/LoginView.vue";
import ForbiddenView from "@/admin/ForbiddenView.vue";
import { getSession, setUnauthorizedHandler } from "@/lib/api";
import { loginRedirectQuery } from "@/lib/auth-utils";
import { decideAdminLanding } from "@/admin/guard";

// 管理端专属视图（KbView/AnalysisView/GenerateView/SettingsView）一律用路由
// 级懒加载（() => import(...)）接线，不在文件顶部静态 import——两个原因：
// ①信息架构上这些视图只属于管理端，不该被使用端的任何代码路径触达；
// ②只有懒加载才会被 Rollup 切成独立 chunk，web-app/scripts/
// check-bundle-isolation.mjs 才能在构建产物 manifest 里找到它们、断言使用端
// 不可达——静态 import 会被直接内联进 admin 入口 chunk，manifest 里不会有
// 独立条目，隔离检查就无从验证（见 spec §3.3、plan F1 第 2 步）。
const router = createRouter({
  history: createWebHistory("/admin"),
  routes: [
    { path: "/login", name: "admin-login", component: LoginView },
    { path: "/forbidden", name: "admin-forbidden", component: ForbiddenView },
    { path: "/", name: "admin-kb", component: () => import("@/views/KbView.vue") },
    { path: "/analysis", name: "admin-analysis", component: () => import("@/views/AnalysisView.vue") },
    { path: "/generate", name: "admin-generate", component: () => import("@/views/GenerateView.vue") },
    { path: "/settings", name: "admin-settings", component: () => import("@/views/SettingsView.vue") },
  ],
});

// 角色落地矩阵（decideAdminLanding 纯函数 + 单测见 src/admin/guard.ts）：
//   无会话        → /admin/login（带 redirect，登录后跳回原目标）
//   viewer        → /admin/forbidden（终态路由，下面的 if 直接放行，不会被
//                    再次判定，从而不会形成循环重定向）
//   editor/admin  → 放行；"/" 渲染知识库页，即管理端首页
router.beforeEach(async (to) => {
  if (to.path === "/login" || to.path === "/forbidden") return true;
  const session = await getSession();
  const landing = decideAdminLanding(session, to.fullPath);
  if (landing.kind === "login") return { path: "/login", query: landing.query };
  if (landing.kind === "forbidden") return { path: "/forbidden" };
  return true;
});

// 401 拦截钩子：与 portal 侧同样的设计取舍（见 src/portal/router.ts 注释）——
// api.ts 是被 router 引入的底层模块，用回调注册而不是反向 import router，
// 保持依赖方向单向。
setUnauthorizedHandler(() => {
  const current = router.currentRoute.value;
  if (current.path === "/login") return;
  router.push({ path: "/login", query: loginRedirectQuery(current.fullPath) });
});

export default router;
