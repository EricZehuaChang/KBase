// 主题切换与持久化：写入 document.documentElement.dataset.theme，
// tokens.css 的 [data-theme="dark"] 选择器据此覆盖变量。
import { ref } from "vue";

export type Theme = "light" | "dark";

const STORAGE_KEY = "kbase-theme";

function readStoredTheme(): Theme {
  const stored = localStorage.getItem(STORAGE_KEY);
  if (stored === "light" || stored === "dark") return stored;
  return window.matchMedia?.("(prefers-color-scheme: dark)").matches ? "dark" : "light";
}

export const theme = ref<Theme>(readStoredTheme());

function applyTheme(value: Theme) {
  document.documentElement.dataset.theme = value;
}

export function setTheme(value: Theme) {
  theme.value = value;
  localStorage.setItem(STORAGE_KEY, value);
  applyTheme(value);
}

export function toggleTheme() {
  setTheme(theme.value === "dark" ? "light" : "dark");
}

// 应用启动时立即生效（main.ts 引入本模块即会执行）。
applyTheme(theme.value);
