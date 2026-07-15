<script setup lang="ts">
// 【管理端】根组件（Vite 入口 admin.html 挂载）。G 重设计对标 Vben v5 /
// Soybean Admin 的管理端骨架共性：侧边栏=logo 区+置顶分组导航+激活左指示条；
// 顶栏=面包屑+主题切换+用户区（用户块从侧栏底部上移到顶栏，主内容区加
// 统一浅底）。功能与路由结构不变：导航无"问答"（在使用端 PortalShell），
// 管理端首页即知识库页；"返回问答"是整页跳转（两端是独立 Vite bundle，
// 无共享 router 实例）。/login、/forbidden 不套导航壳。
import { computed, onMounted, ref } from "vue";
import { useRoute, useRouter } from "vue-router";
import {
  Folder, ScanSearch, FileText, Settings, Sun, Moon, LogOut, ArrowLeft,
  Database,
} from "@lucide/vue";
import { theme, toggleTheme } from "@/lib/theme";
import { getSession, logout, getLicense, currentRole, type Me } from "@/lib/api";
import { roleLabel, roleBadgeClass, canAdminister } from "@/lib/auth-utils";
import { licenseBannerInfo } from "@/lib/settings-utils";
import { Toaster } from "@/components/ui/sonner";

const route = useRoute();
const router = useRouter();

// 分组导航（Vben/Soybean 风）：内容 | 分析 | 系统
const NAV_GROUPS = [
  {
    label: "内容",
    items: [{ path: "/", label: "知识库", icon: Folder }],
  },
  {
    label: "分析",
    items: [
      { path: "/analysis", label: "检索分析", icon: ScanSearch },
      { path: "/generate", label: "生成", icon: FileText },
    ],
  },
  {
    label: "系统",
    items: [{ path: "/settings", label: "设置", icon: Settings }],
  },
] as const;

// 设置入口仅 admin 可见（后端 settings/* 均 require_admin，隐藏只是防呆）；
// 组内全部被过滤时整组（含分组标题）不渲染。
const navGroups = computed(() =>
  NAV_GROUPS.map((g) => ({
    label: g.label,
    items: g.items.filter(
      (item) => item.path !== "/settings" || canAdminister(currentRole.value ?? "")),
  })).filter((g) => g.items.length));

function isActive(path: string): boolean {
  return path === "/" ? route.path === "/" : route.path.startsWith(path);
}

// 顶栏面包屑：当前激活导航项的标签
const currentLabel = computed(() => {
  for (const g of NAV_GROUPS) {
    for (const item of g.items) {
      if (isActive(item.path)) return item.label;
    }
  }
  return "";
});

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
    // 许可证探测失败不阻塞管理端主流程，静默忽略
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
  window.location.href = "/";
}
</script>

<template>
  <router-view v-if="route.path === '/login' || route.path === '/forbidden'" />
  <template v-else>
    <div class="flex h-screen w-full overflow-hidden bg-[var(--bg)] text-[var(--text)]">
      <aside
        class="flex h-full w-[220px] shrink-0 flex-col border-r border-[var(--border)] bg-[var(--surface)]"
      >
        <!-- logo 区：图标徽标 + 产品名 + 端标识 -->
        <div class="flex h-14 shrink-0 items-center gap-2.5 border-b border-[var(--border)] px-4">
          <span class="flex size-8 items-center justify-center rounded-lg bg-[var(--accent)] text-white">
            <Database class="size-4.5" />
          </span>
          <span class="flex flex-col leading-tight">
            <span class="text-[15px] font-semibold tracking-tight">KBase</span>
            <span class="text-[11px] text-[var(--text-3)]">管理工作台</span>
          </span>
        </div>

        <!-- 置顶分组导航：激活项带左侧指示条（Vben 风） -->
        <nav class="flex-1 overflow-y-auto p-2">
          <div v-for="group in navGroups" :key="group.label" class="mb-2">
            <div class="px-3 pb-1 pt-2 text-[11px] font-medium tracking-wide text-[var(--text-3)]">
              {{ group.label }}
            </div>
            <router-link
              v-for="item in group.items"
              :key="item.path"
              :to="item.path"
              custom
              v-slot="{ navigate }"
            >
              <button
                type="button"
                class="relative mb-0.5 flex w-full items-center gap-2.5 rounded-[var(--radius-ctl)] px-3 py-2 text-sm transition-colors"
                :class="isActive(item.path)
                  ? 'bg-[var(--accent-weak)] font-medium text-[var(--accent-text)]'
                  : 'text-[var(--text-2)] hover:bg-[var(--surface-2)] hover:text-[var(--text)]'"
                @click="navigate"
              >
                <span
                  v-if="isActive(item.path)"
                  class="absolute left-0 top-1/2 h-4 w-0.5 -translate-y-1/2 rounded-full bg-[var(--accent)]"
                />
                <component :is="item.icon" class="size-4" />
                <span>{{ item.label }}</span>
              </button>
            </router-link>
          </div>
        </nav>

        <!-- 底部：返回使用端 -->
        <div class="border-t border-[var(--border)] p-2">
          <button
            type="button"
            class="flex w-full items-center gap-2 rounded-[var(--radius-ctl)] px-3 py-2 text-sm text-[var(--text-2)] transition-colors hover:bg-[var(--surface-2)] hover:text-[var(--text)]"
            @click="backToPortal"
          >
            <ArrowLeft class="size-4" />
            返回问答
          </button>
        </div>
      </aside>

      <div class="flex h-full min-w-0 flex-1 flex-col overflow-hidden">
        <!-- 顶栏：面包屑 + 主题切换 + 用户区（Soybean 风） -->
        <header class="flex h-14 shrink-0 items-center justify-between border-b border-[var(--border)] bg-[var(--surface)] px-5">
          <div class="flex items-center gap-1.5 text-sm">
            <span class="text-[var(--text-3)]">管理工作台</span>
            <span v-if="currentLabel" class="text-[var(--text-3)]">/</span>
            <span class="font-medium">{{ currentLabel }}</span>
          </div>
          <div class="flex items-center gap-2">
            <button
              type="button"
              class="rounded-[var(--radius-ctl)] p-2 text-[var(--text-2)] transition-colors hover:bg-[var(--surface-2)]"
              :title="theme === 'dark' ? '切换到浅色模式' : '切换到深色模式'"
              @click="toggleTheme"
            >
              <component :is="theme === 'dark' ? Sun : Moon" class="size-4" />
            </button>
            <div v-if="me" class="flex items-center gap-2 border-l border-[var(--border)] pl-3">
              <span class="text-sm">{{ me.username }}</span>
              <span class="rounded-full px-1.5 py-0.5 text-xs" :class="roleBadgeClass(me.role)">
                {{ roleLabel(me.role) }}
              </span>
              <button
                type="button"
                class="rounded-[var(--radius-ctl)] p-2 text-[var(--text-2)] transition-colors hover:bg-[var(--surface-2)]"
                title="登出"
                @click="handleLogout"
              >
                <LogOut class="size-4" />
              </button>
            </div>
          </div>
        </header>

        <!-- 许可证横幅：trial=提示色、expired/invalid/临近到期=警告色 -->
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
