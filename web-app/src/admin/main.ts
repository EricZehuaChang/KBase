// 【管理端】入口——独立 Vite 入口（admin.html），产出与使用端完全隔离的
// bundle（不共享任何模块级状态，两者是不同 JS 上下文，见 spec 风险条目）。
import { createApp } from "vue";
import "@/styles/main.css";
import "@/lib/theme";
import AdminShell from "@/admin/AdminShell.vue";
import router from "@/admin/router";

createApp(AdminShell).use(router).mount("#app");
