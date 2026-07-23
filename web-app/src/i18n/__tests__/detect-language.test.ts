// detectLanguage 优先级（P2-4 账号级语言偏好）：账号偏好 > localStorage 记忆
// > 浏览器语言前缀 > 默认（zh）。账号偏好由登录后传入（两 Shell），本测试
// 卡住四级优先级与非法码回退，防未来改动打乱顺序。
import { afterEach, beforeEach, describe, expect, it } from "vitest";

import { detectLanguage } from "../index";

const STORAGE_KEY = "kbase-lang"; // 与 i18n/index.ts 内部常量一致

// jsdom 的 navigator.language 是只读 getter，用 defineProperty 覆盖（configurable）。
function setBrowserLang(lang: string): void {
  Object.defineProperty(navigator, "language", { value: lang, configurable: true });
}

describe("detectLanguage 优先级：账号 > localStorage > 浏览器 > 默认", () => {
  beforeEach(() => {
    localStorage.clear();
    setBrowserLang("fr-FR"); // 默认置为不支持语言，浏览器这一级默认不命中
  });
  afterEach(() => {
    localStorage.clear();
  });

  it("账号偏好优先于 localStorage 与浏览器", () => {
    localStorage.setItem(STORAGE_KEY, "zh");
    setBrowserLang("zh-CN");
    expect(detectLanguage("en")).toBe("en");
  });

  it("非法/空账号码回退到 localStorage", () => {
    localStorage.setItem(STORAGE_KEY, "ms");
    expect(detectLanguage("fr")).toBe("ms");
    expect(detectLanguage(null)).toBe("ms");
    expect(detectLanguage(undefined)).toBe("ms");
  });

  it("无账号偏好时 localStorage 优先于浏览器", () => {
    localStorage.setItem(STORAGE_KEY, "en");
    setBrowserLang("ms-MY");
    expect(detectLanguage()).toBe("en");
  });

  it("无账号、无 localStorage 时用浏览器语言前缀", () => {
    setBrowserLang("ms-MY");
    expect(detectLanguage()).toBe("ms");
  });

  it("全不命中回落默认 zh", () => {
    setBrowserLang("fr-FR");
    expect(detectLanguage()).toBe("zh");
  });
});
