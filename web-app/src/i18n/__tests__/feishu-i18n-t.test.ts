// 飞书三处权限指引改用 <I18nT> 具名 slot 键化后的渲染冒烟（P2-1c）。
// 为什么单测：locale 一致性测试只比 key 集合、vite build 只打包 JSON——都不会
// 编译 vue-i18n 消息，而 desc_intro 含 `@机器人`/`@bot`（`@` 是 vue-i18n 链接
// 消息特殊字符），机翻串一旦触发编译错误只会在真机渲染时炸。本测试用真 locale
// JSON 挂载 <I18nT>，三语各跑一遍，卡住：①消息可编译（不抛）②具名 slot 全部
// 填入 ③无残留 `{placeholder}`。
import { mount } from "@vue/test-utils";
import { createI18n, I18nT } from "vue-i18n";
import { defineComponent, h } from "vue";
import { describe, expect, it } from "vitest";

import en from "../locales/en.json";
import ms from "../locales/ms.json";
import zh from "../locales/zh.json";

function renderKeypath(
  locale: string, keypath: string, slots: Record<string, string>,
): string {
  const i18n = createI18n({
    legacy: false, locale, fallbackLocale: "zh", messages: { zh, en, ms },
  });
  const Comp = defineComponent({
    setup() {
      return () => h(
        I18nT as unknown as ReturnType<typeof defineComponent>,
        { keypath, tag: "span", scope: "global" },
        // 每个具名 slot 渲染成标记文本，断言其出现即证明 slot 被填入
        Object.fromEntries(
          Object.entries(slots).map(([name, val]) => [name, () => val])),
      );
    },
  });
  return mount(Comp, { global: { plugins: [i18n] } }).text();
}

const LOCALES = ["zh", "en", "ms"] as const;

describe("飞书富文本 i18n 键渲染（<I18nT> 具名 slot + 特殊字符）", () => {
  it.each(LOCALES)("feishubot.desc_intro 三语渲染全部 code slot（含 @ 特殊字符）[%s]", (locale) => {
    const txt = renderKeypath(locale, "feishubot.desc_intro", {
      scopeMsg: "im:message",
      scopeSend: "im:message:send_as_bot",
      eventKey: "im.message.receive_v1",
    });
    expect(txt).toContain("im:message");
    expect(txt).toContain("im:message:send_as_bot");
    expect(txt).toContain("im.message.receive_v1");
    expect(txt).not.toMatch(/\{scopeMsg\}|\{scopeSend\}|\{eventKey\}/);
  });

  it.each(LOCALES)("feishu.setup.perm_intro 渲染 bold+link slot [%s]", (locale) => {
    const txt = renderKeypath(locale, "feishu.setup.perm_intro", {
      allReadonly: "ALLREADONLY", oneClick: "ONECLICK",
    });
    expect(txt).toContain("ALLREADONLY");
    expect(txt).toContain("ONECLICK");
    expect(txt).not.toMatch(/\{allReadonly\}|\{oneClick\}/);
  });

  it.each(LOCALES)("feishu.setup.perm_note 渲染 review+publish slot [%s]", (locale) => {
    const txt = renderKeypath(locale, "feishu.setup.perm_note", {
      review: "REVIEW", publish: "PUBLISH",
    });
    expect(txt).toContain("REVIEW");
    expect(txt).toContain("PUBLISH");
  });

  it.each(LOCALES)("feishuimport.setup_step2 渲染 allReadonly+publish slot [%s]", (locale) => {
    const txt = renderKeypath(locale, "feishuimport.setup_step2", {
      allReadonly: "ALLRO", publish: "PUB",
    });
    expect(txt).toContain("ALLRO");
    expect(txt).toContain("PUB");
  });

  it.each(LOCALES)("feishuimport.troubleshoot 渲染 publish+addKbMember slot [%s]", (locale) => {
    const txt = renderKeypath(locale, "feishuimport.troubleshoot", {
      publish: "PUB", addKbMember: "ADDKB",
    });
    expect(txt).toContain("PUB");
    expect(txt).toContain("ADDKB");
  });

  it.each(LOCALES)("feishuimport.configured_note 渲染 appId+oneClickPerm slot [%s]", (locale) => {
    const txt = renderKeypath(locale, "feishuimport.configured_note", {
      appId: "cli_APPID", oneClickPerm: "ONECLICKPERM",
    });
    expect(txt).toContain("cli_APPID");
    expect(txt).toContain("ONECLICKPERM");
  });
});
