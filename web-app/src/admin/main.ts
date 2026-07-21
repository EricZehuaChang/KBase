// 【管理端】入口——独立 Vite 入口（admin.html），产出与使用端完全隔离的
// bundle（不共享任何模块级状态，两者是不同 JS 上下文，见 spec 风险条目）。
import { createApp } from "vue";
import "@/styles/main.css";
import "@/lib/theme";
import AdminShell from "@/admin/AdminShell.vue";
import router from "@/admin/router";
import { i18n, initI18n } from "@/i18n";

createApp(AdminShell).use(router).use(i18n).mount("#app");
// 挂载后异步拉 DB 覆盖（不阻塞首屏——基线 messages 已随 bundle 就绪）
initI18n();
