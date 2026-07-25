// UserMenu 顶栏用户菜单冒烟：触发钮只显示登录人用户名+齿轮（其余功能收进
// 下拉）；未登录（me=null）不渲染用户名只留齿轮。弹层内容经 reka Popover
// teleport 渲染，jsdom 下不展开断言（由 vue-tsc 保证绑定正确）。
import { mount } from "@vue/test-utils";
import { createI18n } from "vue-i18n";
import { describe, expect, it } from "vitest";

import UserMenu from "../UserMenu.vue";
import zh from "../../i18n/locales/zh.json";

function make(me: { username: string; role: string } | null, roleText: string) {
  const i18n = createI18n({ legacy: false, locale: "zh", messages: { zh } });
  return mount(UserMenu, {
    props: { me, roleText },
    global: { plugins: [i18n] },
  });
}

describe("UserMenu（顶栏只留登录人）", () => {
  it("触发钮显示用户名（其余功能收进下拉，不在顶栏平铺）", () => {
    const w = make({ username: "admin", role: "superadmin" }, "超级管理员");
    expect(w.text()).toContain("admin");
    // 顶栏触发钮上不平铺角色徽章/改密/登出文案（都在弹层里）
    expect(w.text()).not.toContain("超级管理员");
    expect(w.find("button").attributes("aria-label")).toBe("账号与设置");
  });

  it("me=null（会话未就绪）不渲染用户名，仅齿轮可点", () => {
    const w = make(null, "");
    expect(w.find("button").exists()).toBe(true);
    expect(w.text().trim()).toBe("");
  });
});
