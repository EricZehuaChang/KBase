// src/admin/guard.ts —— 管理端路由守卫的角色落地判定（纯函数，不依赖 router
// 实例/DOM），供 src/admin/router.ts 的 beforeEach 与 vitest 共用（同现状
// src/lib/auth-utils.ts 的设计取向：把可测的决策逻辑从路由/组件里剥离）。
//
// 落地矩阵（spec 2026-07-06-kbase-m5-1-design.md §4）：
//   session === null                         → login      跳 /admin/login（带 redirect）
//   session.role 非 editor/admin（含 viewer） → forbidden  跳 /admin/forbidden（终态路由，
//                                                          该路由自身不再被本守卫拦截，
//                                                          不会形成循环重定向）
//   session.role 为 editor/admin              → allow      放行，"/" 即管理端首页（知识库页）
//
// 权限判定直接复用 auth-utils.ts 的 canManageContent——与"进入工作台"按钮的
// 可见性判定（src/portal/PortalShell.vue）共用同一条规则，避免两处各写一份
// 判断标准、日后改权限矩阵时改漏一处。再次强调：这只决定前端往哪跳，真正
// 挡住越权请求的是后端 require_role（零改动，见 spec §3.3）。
import type { Role } from "@/lib/auth-utils";
import { canManageContent, loginRedirectQuery } from "@/lib/auth-utils";

export interface SessionLike {
  role: Role;
}

export type AdminLanding =
  | { kind: "login"; query: Record<string, string> }
  | { kind: "forbidden" }
  | { kind: "allow" };

/** targetPath：路由 fullPath（相对 /admin base，vue-router 已自动去掉前缀），
 * 用于登录后跳回原目标（redirect query，见 loginRedirectQuery）。*/
export function decideAdminLanding(
  session: SessionLike | null,
  targetPath: string,
): AdminLanding {
  if (session === null) {
    return { kind: "login", query: loginRedirectQuery(targetPath) };
  }
  if (!canManageContent(session.role)) {
    return { kind: "forbidden" };
  }
  return { kind: "allow" };
}
