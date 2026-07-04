import { createRouter, createWebHistory } from "vue-router";
import ChatView from "@/views/ChatView.vue";
import KbView from "@/views/KbView.vue";
import AnalysisView from "@/views/AnalysisView.vue";
import SettingsView from "@/views/SettingsView.vue";

const router = createRouter({
  history: createWebHistory(),
  routes: [
    { path: "/", name: "chat", component: ChatView },
    { path: "/kb", name: "kb", component: KbView },
    { path: "/analysis", name: "analysis", component: AnalysisView },
    { path: "/settings", name: "settings", component: SettingsView },
  ],
});

export default router;
