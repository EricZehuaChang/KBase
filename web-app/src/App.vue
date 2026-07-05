<script setup lang="ts">
// /login 是独立居中卡片页，不套 AppShell（无侧边导航/用户块）——路由级
// 分流：命中 /login 时直接渲染 router-view，否则套 AppShell。
import { useRoute } from "vue-router";
import AppShell from "@/components/AppShell.vue";
import { Toaster } from "@/components/ui/sonner";

const route = useRoute();
</script>

<template>
  <router-view v-if="route.path === '/login'" />
  <AppShell v-else />
  <Toaster />
</template>
