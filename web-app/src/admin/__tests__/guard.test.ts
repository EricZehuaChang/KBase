import { describe, expect, it } from "vitest";
import { decideAdminLanding } from "@/admin/guard";

// 覆盖 guard.ts 文档里写的落地矩阵：无会话 / viewer / editor / admin /
// 未知角色（防御未来新增角色被误判为可放行）五种情形 × 若干目标路径。
describe("decideAdminLanding — 管理端角色落地矩阵", () => {
  it("无会话跳登录页，目标路径进 redirect query", () => {
    expect(decideAdminLanding(null, "/analysis")).toEqual({
      kind: "login",
      query: { redirect: "/analysis" },
    });
  });

  it("无会话访问首页不带多余 redirect（loginRedirectQuery 对 / 的既有语义）", () => {
    expect(decideAdminLanding(null, "/")).toEqual({ kind: "login", query: {} });
  });

  it("viewer 有会话仍判无权限（不是未登录，是登录了但权限不够）", () => {
    expect(decideAdminLanding({ role: "viewer" }, "/")).toEqual({ kind: "forbidden" });
  });

  it("editor 放行", () => {
    expect(decideAdminLanding({ role: "editor" }, "/")).toEqual({ kind: "allow" });
  });

  it("admin 放行，任意管理端路径", () => {
    expect(decideAdminLanding({ role: "admin" }, "/settings")).toEqual({ kind: "allow" });
  });

  it("未知角色兜底按无权限处理", () => {
    expect(decideAdminLanding({ role: "superadmin" }, "/")).toEqual({ kind: "forbidden" });
  });
});
