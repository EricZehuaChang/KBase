// i18n 基线三语 key 一致性（design §9 CI 关键测试）：zh 是源（默认+fallback，
// 全量），en/ms 的 key 集合必须与 zh 完全一致——缺 key = 漏译（运行时回落 zh，
// 客户看到中文），多 key = 脏数据。机翻脚本已按源对齐（scripts/
// i18n_machine_translate.py「对齐源 key 集合」），本测试卡死对齐防未来漂移。
import { describe, expect, it } from "vitest";

import en from "../locales/en.json";
import ms from "../locales/ms.json";
import zh from "../locales/zh.json";

// 扁平化嵌套 JSON 成点分 key（与运行时 unflatten 互逆，见 i18n/index.ts）。
function flatKeys(obj: Record<string, unknown>, prefix = ""): string[] {
  return Object.entries(obj).flatMap(([k, v]) => {
    const key = prefix ? `${prefix}.${k}` : k;
    return v && typeof v === "object" && !Array.isArray(v)
      ? flatKeys(v as Record<string, unknown>, key)
      : [key];
  });
}

describe("locale key consistency (zh source of truth)", () => {
  const zhKeys = [...new Set(flatKeys(zh))].sort();

  it("en has exactly the same keys as zh", () => {
    expect([...new Set(flatKeys(en))].sort()).toEqual(zhKeys);
  });

  it("ms has exactly the same keys as zh", () => {
    expect([...new Set(flatKeys(ms))].sort()).toEqual(zhKeys);
  });
});
