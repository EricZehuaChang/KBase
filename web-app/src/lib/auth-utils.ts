// src/lib/auth-utils.ts —— 登录/会话相关纯函数（不依赖 router 实例/DOM），
// 供 LoginView、路由守卫共用，vitest 直接单测。

export type Role = "admin" | "editor" | "viewer" | string;

interface RouteLike {
  query: Record<string, unknown>;
}

/** 判断字符串是否是"安全的"站内相对路径——必须以单个 "/" 开头，且不是协议
 * 相对（"//host/..."）或绝对 URL，防止开放重定向（redirect=https://evil...）。*/
function isSafeInternalPath(path: string): boolean {
  return path.startsWith("/") && !path.startsWith("//");
}

/** 登录成功后应跳转的目标路径：取自 route.query.redirect（数组取第一个），
 * 校验为安全的站内路径且不指向 /login 本身，否则兜底 "/"。 */
export function redirectTarget(route: RouteLike): string {
  const raw = route.query.redirect;
  const target = Array.isArray(raw) ? raw[0] : raw;
  if (typeof target !== "string" || !isSafeInternalPath(target)) return "/";
  if (target === "/login" || target.startsWith("/login?") || target.startsWith("/login/")) return "/";
  return target;
}

/** 路由守卫拦截到无会话时，构造跳转 /login 所需的 query：目标路径本身就是
 * /login 或首页时不附带 redirect（避免多余的 ?redirect=/ 或自指）。 */
export function loginRedirectQuery(targetPath: string): Record<string, string> {
  if (targetPath === "/login" || targetPath === "/") return {};
  return { redirect: targetPath };
}

const ROLE_LABELS: Record<string, string> = {
  admin: "管理员",
  editor: "编辑者",
  viewer: "查看者",
};

/** 角色枚举 → 中文标签；未知角色原样返回（不遮盖后端新增角色）。 */
export function roleLabel(role: Role): string {
  return ROLE_LABELS[role] ?? role;
}

/** 角色徽章配色：admin 用强调色突出最高权限，其余角色用中性色。 */
export function roleBadgeClass(role: Role): string {
  return role === "admin"
    ? "bg-[var(--accent-weak)] text-[var(--accent-text)]"
    : "bg-[var(--surface-2)] text-[var(--text-2)]";
}
