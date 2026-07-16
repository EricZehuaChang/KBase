// lib/api/core.ts —— HTTP 基座与会话核心：req/jsonInit、401 拦截、
// 登录/登出/会话缓存/当前角色、SSO 探测。
// 会话缓存与 401 拦截互相调用，必须同模块（拆开会形成 http↔auth 循环依赖）。
import { ref } from "vue";

// 401 拦截钩子：router 守卫在启动时注册一个回调（跳转 /login），本模块不
// 直接 import router——避免 api ↔ router 循环依赖。未注册时（如单测）
// 拦截器仅清缓存、不做跳转。登录接口本身的 401（用户名/密码错）不走这条
// 拦截逻辑，由 LoginView 自己捕获展示错误——见 login() 用 skipAuthRedirect。
let onUnauthorized: (() => void) | null = null;

export function setUnauthorizedHandler(handler: (() => void) | null): void {
  onUnauthorized = handler;
}

/** 统一处理一次 401（会话失效）：清会话缓存并触发注册的跳转回调。返回是否
 * 有回调被触发——调用方（如 SSE 流的手工 fetch 分支）据此决定是否还需展示
 * 兜底错误气泡：有回调时页面已在跳登录页，气泡冗余；无回调（如单测/未注册）
 * 时仍应展示，避免错误无声消失。req() 与 useChat 的 raw-fetch 分支共用本函数，
 * 保证两条 401 路径行为一致。 */
export function handleUnauthorized(): boolean {
  clearSessionCache();
  if (onUnauthorized) {
    onUnauthorized();
    return true;
  }
  return false;
}

/** 各域模块共用的请求基座（barrel 导出后对组件也可见，但组件应优先用
 * 域函数而不是裸调 req）。 */
export async function req<T>(path: string, init?: RequestInit,
                             opts?: { skipAuthRedirect?: boolean }): Promise<T> {
  const res = await fetch(path, { credentials: "include", ...init });
  if (!res.ok) {
    if (res.status === 401 && !opts?.skipAuthRedirect) {
      handleUnauthorized();
    }
    const text = await res.text();
    let detail: string | undefined;
    try {
      detail = JSON.parse(text)?.detail;
    } catch {
      // 非 JSON 响应体，原样用 text 兜底
    }
    // 兜底状态码文案：网关错误（如 502）响应体可能为空，空字符串消息的 Error
    // 在调用方 v-if 判断中是假值，会让错误态"消失"，统一在这里保证消息非空。
    throw new Error(detail || text || `请求失败 (${res.status})`);
  }
  if (res.status === 204) return undefined as T;
  return res.json() as Promise<T>;
}

export function jsonInit(body: unknown, method = "POST"): RequestInit {
  return {
    method,
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify(body),
  };
}

// ---- 认证（M4-1 G5）----

export interface Me {
  username: string;
  role: string;
  // 账号邮箱（忘记密码重置用）；API Key 身份为 null。前端首登据此弹引导补录
  email?: string | null;
}

export function login(username: string, password: string): Promise<Me> {
  // skipAuthRedirect：登录失败也是 401，但那是"密码错"不是"会话过期"，不该
  // 触发全局跳转 /login（本来就在 /login 页）——由 LoginView 捕获异常自己展示。
  return req("/api/auth/login", jsonInit({ username, password }), { skipAuthRedirect: true });
}

export async function logout(): Promise<{ ok: boolean }> {
  const r = await req<{ ok: boolean }>("/api/auth/logout", { method: "POST" });
  clearSessionCache();
  return r;
}

export function me(): Promise<Me> {
  return req("/api/auth/me");
}

// M6-8 企业 SSO：登录页据此显示"企业账号登录"入口
export function getSsoStatus(): Promise<{ enabled: boolean }> {
  return req("/api/auth/sso/status");
}

// 自助改密（登录用户，旧密码复核）；API Key 身份无账号密码语义（后端 403）
export function changePassword(oldPassword: string, newPassword: string): Promise<{ ok: boolean }> {
  return req("/api/auth/change-password",
             jsonInit({ old_password: oldPassword, new_password: newPassword }));
}

// 登录用户维护自己的邮箱（首登引导填写，用于忘记密码重置）
export function updateProfile(email: string): Promise<{ ok: boolean }> {
  return req("/api/auth/profile", jsonInit({ email }, "PUT"));
}

// 忘记密码：无论账号是否存在都返回同一句话（后端防枚举）
export function forgotPassword(account: string): Promise<{ ok: boolean; message: string }> {
  return req("/api/auth/forgot", jsonInit({ account }));
}

// 凭邮件里的一次性 token 设新密码（登录页 ?reset_token= 流程）
export function resetPassword(token: string, newPassword: string): Promise<{ ok: boolean }> {
  return req("/api/auth/reset", jsonInit({ token, new_password: newPassword }));
}

// 会话探测结果模块级缓存：路由守卫每次导航都要确认"有没有会话"，但不该每次
// 都打一发 /api/auth/me——同一个会话生命周期内缓存结果，登出/401 时清空
// （见 setUnauthorizedHandler 拦截器与 clearSessionCache 调用点）。
let sessionCache: Promise<Me | null> | null = null;

// 全局响应式当前角色（M4-1 G6 角色门控用）：AppShell/各 View 据此隐藏入口。
// 后端已用 require_role 强制校验，这是纯 UX 防呆；未登录或探测失败时为 null，
// 门控函数（canManageContent/canAdminister）对 null 视同无权限。
export const currentRole = ref<string | null>(null);

/** 探测当前会话：命中缓存直接返回；未命中时调用 /api/auth/me 并缓存结果
 * （含"确认无会话"的 null，避免连续未登录状态下的重复探测请求）。 */
export function getSession(): Promise<Me | null> {
  if (sessionCache === null) {
    sessionCache = me().catch(() => null);
    sessionCache.then((session) => {
      currentRole.value = session?.role ?? null;
    });
  }
  return sessionCache;
}

/** 清空会话缓存：401 拦截器与 logout() 成功后调用，强制下次 getSession()
 * 重新探测。同时清空 currentRole——门控 UI 应立即隐藏而不是等下次探测。 */
export function clearSessionCache(): void {
  sessionCache = null;
  currentRole.value = null;
}
