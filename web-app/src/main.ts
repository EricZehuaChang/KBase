import { createApp } from "vue";
import "@/styles/main.css";
import "@/lib/theme";
import App from "@/App.vue";
import router from "@/router";

createApp(App).use(router).mount("#app");
