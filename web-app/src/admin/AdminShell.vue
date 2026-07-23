<script setup lang="ts">
// 【管理端】根组件（Vite 入口 admin.html 挂载）。G 重设计对标 Vben v5 /
// Soybean Admin 的管理端骨架共性：侧边栏=logo 区+置顶分组导航+激活左指示条；
// 顶栏=面包屑+主题切换+用户区（用户块从侧栏底部上移到顶栏，主内容区加
// 统一浅底）。功能与路由结构不变：导航无"问答"（在使用端 PortalShell），
// 管理端首页即知识库页；"返回问答"是整页跳转（两端是独立 Vite bundle，
// 无共享 router 实例）。/login、/forbidden 不套导航壳。
import { computed, onMounted, ref } from "vue";
import { useRoute, useRouter } from "vue-router";
import { useI18n } from "vue-i18n";
import {
  Folder, ScanSearch, FileText, Settings, Sun, Moon, LogOut, ArrowLeft,
  Database, KeyRound, Languages,
} from "@lucide/vue";
import ChangePasswordDialog from "@/components/ChangePasswordDialog.vue";
import EmailPromptDialog from "@/components/EmailPromptDialog.vue";
import LanguagePicker from "@/components/LanguagePicker.vue";
import { theme, toggleTheme } from "@/lib/theme";
import { getSession, logout, getLicense, currentRole, type Me } from "@/lib/api";
import { setLanguage } from "@/i18n";
import { roleBadgeClass, canAdminister } from "@/lib/auth-utils";
import { licenseBannerInfo } from "@/lib/settings-utils";
import { Toaster } from "@/components/ui/sonner";

const route = useRoute();
const router = useRouter();
const { t } = useI18n();

// 分组导航（Vben/Soybean 风）：内容 | 分析 | 系统。label 存 i18n key，渲染 t()。
const NAV_GROUPS = [
  {
    label: "admin.nav_content",
    items: [{ path: "/", label: "admin.nav_kb", icon: Folder }],
  },
  {
    label: "admin.nav_analysis",
    items: [
      { path: "/analysis", label: "admin.nav_retrieval", icon: ScanSearch },
      { path: "/generate", label: "admin.nav_generate", icon: FileText },
    ],
  },
  {
    label: "admin.nav_system",
    items: [
      { path: "/settings", label: "admin.nav_settings", icon: Settings },
      { path: "/translations", label: "admin.nav_translations", icon: Languages },
    ],
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

// 顶栏面包屑：当前激活导航项的标签（i18n key，渲染时 t()）
const currentLabel = computed(() => {
  for (const g of NAV_GROUPS) {
    for (const item of g.items) {
      if (isActive(item.path)) return item.label;
    }
  }
  return "";
});

const me = ref<Me | null>(null);
const emailPromptOpen = ref(false);
onMounted(async () => {
  me.value = await getSession();
  // P2-4 账号级语言偏好：账号设过就切过去（覆盖启动本地检测），跨设备一致
  // 母语。persistAccount:false——读账号→应用，非手动切换，不回写。未设置则
  // 维持本地检测。
  if (me.value?.language) void setLanguage(me.value.language, { persistAccount: false });
  // 首登邮箱引导（与 PortalShell 同规则）："稍后再说"记 sessionStorage，
  // 本次浏览器会话内两端都不再弹
  if (me.value && me.value.email === null
      && !sessionStorage.getItem("kbase_email_prompt_dismissed")) {
    emailPromptOpen.value = true;
  }
});

// 角色标签本地化：common.role.<role>，未知角色回落原始码（同 PortalShell）
const roleText = computed(() => {
  const role = me.value?.role;
  if (!role) return "";
  const key = `common.role.${role}`;
  const translated = t(key);
  return translated !== key ? translated : role;
});

function handleEmailSaved(email: string) {
  // 原地改：getSession 缓存的是同一引用，保证后续读缓存不再触发弹窗
  if (me.value) me.value.email = email;
}

const bannerInfo = ref<ReturnType<typeof licenseBannerInfo>>(null);
const bannerDismissed = ref(false);
onMounted(async () => {
  try {
    bannerInfo.value = licenseBannerInfo(await getLicense());
  } catch {
    // 许可证探测失败不阻塞管理端主流程，静默忽略
  }
});

const changePwOpen = ref(false);

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
            <span class="text-[11px] text-[var(--text-3)]">{{ t("admin.workbench") }}</span>
          </span>
        </div>

        <!-- 置顶分组导航：激活项带左侧指示条（Vben 风） -->
        <nav class="flex-1 overflow-y-auto p-2">
          <div v-for="group in navGroups" :key="group.label" class="mb-2">
            <div class="px-3 pb-1 pt-2 text-[11px] font-medium tracking-wide text-[var(--text-3)]">
              {{ t(group.label) }}
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
                <span>{{ t(item.label) }}</span>
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
            {{ t("admin.back_to_portal") }}
          </button>
        </div>
      </aside>

      <div class="flex h-full min-w-0 flex-1 flex-col overflow-hidden">
        <!-- 顶栏：面包屑 + 主题切换 + 用户区（Soybean 风） -->
        <header class="flex h-14 shrink-0 items-center justify-between border-b border-[var(--border)] bg-[var(--surface)] px-5">
          <div class="flex items-center gap-1.5 text-sm">
            <span class="text-[var(--text-3)]">{{ t("admin.workbench") }}</span>
            <span v-if="currentLabel" class="text-[var(--text-3)]">/</span>
            <span class="font-medium">{{ currentLabel ? t(currentLabel) : "" }}</span>
          </div>
          <div class="flex items-center gap-2">
            <LanguagePicker />
            <button
              type="button"
              class="rounded-[var(--radius-ctl)] p-2 text-[var(--text-2)] transition-colors hover:bg-[var(--surface-2)]"
              :title="t(theme === 'dark' ? 'portal.topbar.to_light' : 'portal.topbar.to_dark')"
              @click="toggleTheme"
            >
              <component :is="theme === 'dark' ? Sun : Moon" class="size-4" />
            </button>
            <div v-if="me" class="flex items-center gap-2 border-l border-[var(--border)] pl-3">
              <button
                type="button"
                class="rounded-[var(--radius-ctl)] p-2 text-[var(--text-2)] transition-colors hover:bg-[var(--surface-2)]"
                :title="t('portal.topbar.change_pw')"
                @click="changePwOpen = true"
              >
                <KeyRound class="size-4" />
              </button>
              <span class="text-sm">{{ me.username }}</span>
              <span class="rounded-full px-1.5 py-0.5 text-xs" :class="roleBadgeClass(me.role)">
                {{ roleText }}
              </span>
              <button
                type="button"
                class="rounded-[var(--radius-ctl)] p-2 text-[var(--text-2)] transition-colors hover:bg-[var(--surface-2)]"
                :title="t('portal.topbar.logout')"
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
          <span>{{ t(bannerInfo.messageKey, bannerInfo.messageParams ?? {}) }}</span>
          <button
            type="button"
            class="shrink-0 rounded-[var(--radius-ctl)] px-1.5 py-0.5 hover:bg-black/5"
            :aria-label="t('admin.dismiss_banner')"
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
  <ChangePasswordDialog v-model:open="changePwOpen" />
  <EmailPromptDialog v-model:open="emailPromptOpen" @saved="handleEmailSaved" />
  <Toaster />
</template>
