// lib/api/i18n.ts —— i18n 译文覆盖管理（管理端「多语言」页，spec §6）。
// 基线译文在 locales/*.json 随包走；这里只管运营在后台改过的 DB 覆盖增量：
// 读全部语言覆盖（与基线合并展示已改项）+ 写单条覆盖（空值=删除覆盖回落基线）。
import { jsonInit, req } from "./core";

/** 全部语言的 DB 覆盖 {lang: {key: value}}——喂管理页标出哪些 key 已被改过。 */
export function getAllI18nOverrides(): Promise<Record<string, Record<string, string>>> {
  return req("/api/i18n");
}

/** 写某语言某 key 的覆盖（value 空串=删除覆盖，该 key 回落基线）。 */
export function putI18nOverride(
  lang: string, key: string, value: string,
): Promise<{ ok: boolean; result: "set" | "deleted" }> {
  return req("/api/i18n", jsonInit({ lang, key, value }, "PUT"));
}
