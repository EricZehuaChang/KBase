// 支持语言清单——**加新语言的唯一改动点**:新增一门语言 = 这里加一项 +
// 放一份 locales/<code>.json（可用 scripts/i18n_machine_translate.py 机翻生成）。
// 切换器、检测、翻译管理页都从这份清单派生，零散落。
export interface Language {
  code: string; // BCP-47 简码，对齐 locales 文件名与 navigator.language 前缀
  name: string; // 母语自称（切换器里各显本语言名，用户一眼认得）
}

// 顺序即切换器展示顺序。中文在首（源语言）。
export const LANGUAGES: Language[] = [
  { code: "zh", name: "中文" },
  { code: "en", name: "English" },
  { code: "ms", name: "Bahasa Melayu" },
];

// 默认 + fallback 语言：中文是全量源，任一 key 缺其他语言译文时回落到它，
// 保证界面永不出现裸 key 名或空白。
export const DEFAULT_LANG = "zh";

export const SUPPORTED_CODES: readonly string[] = LANGUAGES.map((l) => l.code);
