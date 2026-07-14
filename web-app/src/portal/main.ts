// 【使用端】入口——独立 Vite 入口（index.html），产出与管理端完全隔离的
// bundle（不共享任何模块级状态，两者是不同 JS 上下文，见 spec 风险条目）。
import { createApp } from "vue";
import "@/styles/main.css";
import "@/lib/theme";
import PortalShell from "@/portal/PortalShell.vue";
import router from "@/portal/router";

createApp(PortalShell).use(router).mount("#app");
