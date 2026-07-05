<script setup lang="ts">
import { onMounted, ref } from "vue";
import { useRoute, useRouter } from "vue-router";
import { MessageCircle, Folder, ScanSearch, FileText, Settings, Sun, Moon, LogOut } from "@lucide/vue";
import { theme, toggleTheme } from "@/lib/theme";
import { getSession, logout, type Me } from "@/lib/api";
import { roleLabel, roleBadgeClass } from "@/lib/auth-utils";

const route = useRoute();
const router = useRouter();

const navItems = [
  { path: "/", label: "问答", icon: MessageCircle },
  { path: "/kb", label: "知识库", icon: Folder },
  { path: "/analysis", label: "检索分析", icon: ScanSearch },
  { path: "/generate", label: "生成", icon: FileText },
  { path: "/settings", label: "设置", icon: Settings },
] as const;

function isActive(path: string): boolean {
  return path === "/" ? route.path === "/" : route.path.startsWith(path);
}

// 路由守卫已经确保能进到 AppShell 时会话必然存在，这里复用同一份缓存
// （getSession）读用户名/角色展示，不再单独发一次请求。
const me = ref<Me | null>(null);
onMounted(async () => {
  me.value = await getSession();
});

async function handleLogout() {
  try {
    await logout();
  } finally {
    // 无论 logout 请求是否成功都清本地态并跳登录页——服务端 Cookie 失效或
    // 网络失败都不该把用户卡在"看起来已登出但页面没反应"的状态。
    await router.push("/login");
  }
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

      <!-- 底部用户块：用户名 + 角色徽章 + 登出 -->
      <div v-if="me" class="flex items-center justify-between gap-2 border-t border-[var(--border)] p-3">
        <div class="flex min-w-0 flex-col gap-1">
          <span class="truncate text-sm text-[var(--text)]">{{ me.username }}</span>
          <span
            class="w-fit rounded-full px-1.5 py-0.5 text-xs"
            :class="roleBadgeClass(me.role)"
          >
            {{ roleLabel(me.role) }}
          </span>
        </div>
        <button
          type="button"
          class="shrink-0 rounded-[var(--radius-ctl)] p-2 text-[var(--text-2)] transition-colors hover:bg-[var(--surface-2)]"
          title="登出"
          @click="handleLogout"
        >
          <LogOut class="size-4" />
        </button>
      </div>
    </aside>

    <main class="h-full min-w-0 flex-1 overflow-y-auto">
      <router-view />
    </main>
  </div>
</template>
