// 产品导览文案全量渲染冒烟。为什么单测：locale 一致性测试只比 key 集合、
// demo-tour 配置测试只查 key 存在——都不会编译 vue-i18n 消息。真机踩坑：
// tour.analysis.desc 的 "hit@k" 裸 `@`（链接消息特殊字符）在导览走到第 9 步
// 渲染时抛 SyntaxError，DemoTour 组件整个被卸载（deploy48 存量 bug）。本测试
// 把 tour.* 全部叶子 key 三语逐条 t() 渲染，任何一条编译不过即红。
import { createI18n } from "vue-i18n";
import { describe, expect, it } from "vitest";

import en from "../locales/en.json";
import ms from "../locales/ms.json";
import zh from "../locales/zh.json";

const LOCALES = { zh, en, ms } as const;

/** 收集 tour.* 下的全部叶子 key（点分路径）。 */
function leafKeys(obj: unknown, prefix: string): string[] {
  if (typeof obj === "string") return [prefix];
  if (obj && typeof obj === "object") {
    return Object.entries(obj).flatMap(([k, v]) => leafKeys(v, `${prefix}.${k}`));
  }
  return [];
}

describe("导览文案渲染冒烟（vue-i18n 消息编译）", () => {
  it.each(Object.keys(LOCALES) as (keyof typeof LOCALES)[])(
    "tour.* 全部 key 可编译渲染 [%s]", (locale) => {
      const i18n = createI18n({
        legacy: false, locale, fallbackLocale: "zh", messages: { zh, en, ms },
      });
      const keys = leafKeys(LOCALES[locale].tour, "tour");
      expect(keys.length).toBeGreaterThan(30);
      for (const key of keys) {
        // 消息编译在首次 t() 时进行，特殊字符（裸 @、坏占位符）在这里抛
        const rendered = i18n.global.t(key);
        expect(rendered, key).toBeTruthy();
      }
    });

  it("analysis.desc 的 {'@'} 转义渲染回 hit@k 字面量", () => {
    const i18n = createI18n({
      legacy: false, locale: "zh", fallbackLocale: "zh", messages: { zh, en, ms },
    });
    expect(i18n.global.t("tour.analysis.desc")).toContain("hit@k");
  });
});
