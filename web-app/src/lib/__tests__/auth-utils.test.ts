import { describe, expect, it } from "vitest";
import { redirectTarget, loginRedirectQuery, roleLabel, roleBadgeClass } from "../auth-utils";

describe("redirectTarget", () => {
  it("query.redirect 存在且为站内路径时使用它", () => {
    expect(redirectTarget({ query: { redirect: "/kb" } })).toBe("/kb");
  });

  it("无 redirect 时兜底 /", () => {
    expect(redirectTarget({ query: {} })).toBe("/");
  });

  it("redirect 是数组时取第一个", () => {
    expect(redirectTarget({ query: { redirect: ["/generate", "/kb"] } })).toBe("/generate");
  });

  it("redirect 指向站外地址（协议相对/绝对 URL）时兜底 /，防开放重定向", () => {
    expect(redirectTarget({ query: { redirect: "https://evil.example.com" } })).toBe("/");
    expect(redirectTarget({ query: { redirect: "//evil.example.com" } })).toBe("/");
  });

  it("redirect 是 /login 自身时兜底 /，避免登录后又跳回登录页", () => {
    expect(redirectTarget({ query: { redirect: "/login" } })).toBe("/");
    expect(redirectTarget({ query: { redirect: "/login?redirect=/kb" } })).toBe("/");
  });
});

describe("loginRedirectQuery", () => {
  it("非 /login 路径生成 redirect 查询参数", () => {
    expect(loginRedirectQuery("/kb")).toEqual({ redirect: "/kb" });
  });

  it("目标已是 /login 或 / 时不附带 redirect", () => {
    expect(loginRedirectQuery("/login")).toEqual({});
    expect(loginRedirectQuery("/")).toEqual({});
  });
});

describe("roleLabel", () => {
  it("映射三种角色到中文标签", () => {
    expect(roleLabel("admin")).toBe("管理员");
    expect(roleLabel("editor")).toBe("编辑者");
    expect(roleLabel("viewer")).toBe("查看者");
  });

  it("未知角色原样返回", () => {
    expect(roleLabel("weird")).toBe("weird");
  });
});

describe("roleBadgeClass", () => {
  it("admin 使用强调色，其余使用中性色", () => {
    expect(roleBadgeClass("admin")).toBe("bg-[var(--accent-weak)] text-[var(--accent-text)]");
    expect(roleBadgeClass("editor")).toBe("bg-[var(--surface-2)] text-[var(--text-2)]");
    expect(roleBadgeClass("viewer")).toBe("bg-[var(--surface-2)] text-[var(--text-2)]");
  });
});
