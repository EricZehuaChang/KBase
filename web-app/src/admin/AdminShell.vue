<script setup lang="ts">
// 【管理端】根组件（Vite 入口 admin.html 挂载）。≈ 分端改造前的 AppShell.vue，
// 差异：①导航去掉"问答"（M5-1 F1 把问答收进使用端 PortalShell，见 spec §3）；
// ②知识库入口路径从原来的 "/kb" 改成 "/"（管理端首页即知识库页，落地矩阵
// 见 src/admin/guard.ts）；③新增"返回问答"整页跳转入口——两端是各自独立的
// Vite bundle，没有共享 router 实例可以 push 过去，只能 location 整页导航。
// /login、/forbidden 两个路由不套这层导航壳（分别是登录卡片页与无权限终态
// 页），逻辑与原 App.vue 的路由分流一致，这里内联在同一个文件里。
import { computed, onMounted, ref } from "vue";
import { useRoute, useRouter } from "vue-router";
import { Folder, ScanSearch, FileText, Settings, Sun, Moon, LogOut, ArrowLeft } from "@lucide/vue";
import { theme, toggleTheme } from "@/lib/theme";
import { getSession, logout, getLicense, currentRole, type Me } from "@/lib/api";
import { roleLabel, roleBadgeClass, canAdminister } from "@/lib/auth-utils";
import { licenseBannerInfo } from "@/lib/settings-utils";
import { Toaster } from "@/components/ui/sonner";

const route = useRoute();
const router = useRouter();

const ALL_NAV_ITEMS = [
  { path: "/", label: "知识库", icon: Folder },
  { path: "/analysis", label: "检索分析", icon: ScanSearch },
  { path: "/generate", label: "生成", icon: FileText },
  { path: "/settings", label: "设置", icon: Settings },
] as const;

// 设置入口仅 admin 可见（editor/viewer 均隐藏——后端 settings/* 与用户管理
// 端点都是 require_admin，这里隐藏只是防呆，不替代后端校验），沿用现状逻辑。
const navItems = computed(() =>
  ALL_NAV_ITEMS.filter((item) => item.path !== "/settings" || canAdminister(currentRole.value ?? "")));

function isActive(path: string): boolean {
  return path === "/" ? route.path === "/" : route.path.startsWith(path);
}

const me = ref<Me | null>(null);
onMounted(async () => {
  me.value = await getSession();
});

const bannerInfo = ref<ReturnType<typeof licenseBannerInfo>>(null);
const bannerDismissed = ref(false);
onMounted(async () => {
  try {
    bannerInfo.value = licenseBannerInfo(await getLicense());
  } catch {
    // 许可证探测失败不阻塞管理端主流程，静默忽略（与现状 AppShell 一致）
  }
});

async function handleLogout() {
  try {
    await logout();
  } finally {
    await router.push("/login");
  }
}

function backToPortal() {
  // 整页跳转（不是 router.push）：使用端是另一个 Vite 入口的独立 bundle，
  // 没有共享的 router 实例可以导航过去。
  window.location.href = "/";
}
</script>

<template>
  <router-view v-if="route.path === '/login' || route.path === '/forbidden'" />
  <template v-else>
    <div class="flex h-screen w-full overflow-hidden bg-[var(--bg)] text-[var(--text)]">
      <aside
        class="flex h-full w-[208px] shrink-0 flex-col border-r border-[var(--border)] bg-[var(--surface)]"
      >
        <!-- 顶部 logo + 返回问答 -->
        <div class="flex h-14 items-center px-4 text-lg font-semibold tracking-tight">
          KBase
        </div>
        <button
          type="button"
          class="mx-2 mb-2 flex items-center gap-1.5 rounded-[var(--radius-ctl)] px-2 py-1.5 text-xs text-[var(--text-2)] transition-colors hover:bg-[var(--surface-2)]"
          @click="backToPortal"
        >
          <ArrowLeft class="size-3.5" />
          返回问答
        </button>

        <div class="flex-1" />

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

      <div class="flex h-full min-w-0 flex-1 flex-col overflow-hidden">
        <!-- 许可证横幅：trial=提示色、expired/invalid=警告色（tokens 语义色） -->
        <div
          v-if="bannerInfo && !bannerDismissed"
          class="flex shrink-0 items-center justify-between gap-3 px-4 py-1.5 text-xs"
          :class="bannerInfo.tone === 'warn'
            ? 'bg-[var(--warn-weak)] text-[var(--warn)]'
            : 'bg-[var(--accent-weak)] text-[var(--accent-text)]'"
        >
          <span>{{ bannerInfo.message }}</span>
          <button
            type="button"
            class="shrink-0 rounded-[var(--radius-ctl)] px-1.5 py-0.5 hover:bg-black/5"
            aria-label="关闭提示"
            @click="bannerDismissed = true"
          >
            ×
          </button>
        </div>

        <main class="min-h-0 flex-1 overflow-y-auto">
          <router-view />
        </main>
      </div>
    </div>
  </template>
  <Toaster />
</template>
