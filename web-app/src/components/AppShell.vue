<script setup lang="ts">
import { useRoute } from "vue-router";
import { MessageCircle, Folder, ScanSearch, Settings, Sun, Moon } from "@lucide/vue";
import { theme, toggleTheme } from "@/lib/theme";

const route = useRoute();

const navItems = [
  { path: "/", label: "问答", icon: MessageCircle },
  { path: "/kb", label: "知识库", icon: Folder },
  { path: "/analysis", label: "检索分析", icon: ScanSearch },
  { path: "/settings", label: "设置", icon: Settings },
] as const;

function isActive(path: string): boolean {
  return path === "/" ? route.path === "/" : route.path.startsWith(path);
}
</script>

<template>
  <div class="flex h-screen w-full overflow-hidden bg-[var(--bg)] text-[var(--text)]">
    <aside
      class="flex h-full w-[208px] shrink-0 flex-col border-r border-[var(--border)] bg-[var(--surface)]"
    >
      <!-- 顶部 logo -->
      <div class="flex h-14 items-center px-4 text-lg font-semibold tracking-tight">
        KBase
      </div>

      <!-- 中部插槽：问答页放会话列表（各 View 通过 Teleport to="#sidebar-slot" 注入） -->
      <div id="sidebar-slot" class="flex flex-1 flex-col overflow-y-auto px-2">
        <slot />
      </div>

      <!-- 底部导航 -->
      <nav class="flex flex-col gap-1 border-t border-[var(--border)] p-2">
        <router-link
          v-for="item in navItems"
          :key="item.path"
          :to="item.path"
          custom
          v-slot="{ navigate }"
        >
          <button
            type="button"
            class="flex items-center gap-2 rounded-[var(--radius-ctl)] px-3 py-2 text-sm transition-colors"
            :class="isActive(item.path)
              ? 'bg-[var(--accent-weak)] text-[var(--accent-text)]'
              : 'text-[var(--text-2)] hover:bg-[var(--surface-2)]'"
            @click="navigate"
          >
            <component :is="item.icon" class="size-4" />
            <span>{{ item.label }}</span>
          </button>
        </router-link>

        <button
          type="button"
          class="mt-1 flex items-center gap-2 rounded-[var(--radius-ctl)] px-3 py-2 text-sm text-[var(--text-2)] transition-colors hover:bg-[var(--surface-2)]"
          @click="toggleTheme"
        >
          <component :is="theme === 'dark' ? Sun : Moon" class="size-4" />
          <span>{{ theme === "dark" ? "浅色模式" : "深色模式" }}</span>
        </button>
      </nav>
    </aside>

    <main class="h-full min-w-0 flex-1 overflow-y-auto">
      <router-view />
    </main>
  </div>
</template>
