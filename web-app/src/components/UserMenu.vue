<script setup lang="ts">
// 顶栏用户菜单（portal + admin 复用）：顶栏只保留登录人用户名 + 设置齿轮，
// 其余功能（角色徽章/语言切换/主题/修改密码/登出）全部收进下拉——
// 用户反馈图标横排一串太散，收敛为一个入口。
// 改密对话框/登出逻辑留在 Shell（emit 上抛），语言与主题为全局纯前端状态
// 本组件直接操作。动作项点击即收起下拉（菜单语义）。
import { ref } from "vue";
import { useI18n } from "vue-i18n";
import { KeyRound, LogOut, Moon, Settings, Sun } from "@lucide/vue";
import { Popover, PopoverContent, PopoverTrigger } from "@/components/ui/popover";
import LanguagePicker from "@/components/LanguagePicker.vue";
import { theme, toggleTheme } from "@/lib/theme";
import { roleBadgeClass } from "@/lib/auth-utils";
import type { Me } from "@/lib/api";

defineProps<{ me: Me | null; roleText: string }>();
const emit = defineEmits<{ changePassword: []; logout: [] }>();
const { t } = useI18n();

const open = ref(false);

function act(event: "changePassword" | "logout") {
  open.value = false;
  if (event === "changePassword") emit("changePassword");
  else emit("logout");
}

// 菜单行公共样式（图标+文字整行可点）
const ROW = "flex w-full items-center gap-2 rounded-[var(--radius-ctl)] px-2 "
  + "py-1.5 text-sm text-[var(--text)] transition-colors hover:bg-[var(--surface-2)]";
</script>

<template>
  <Popover v-model:open="open">
    <PopoverTrigger as-child>
      <button
        type="button"
        class="flex items-center gap-1.5 rounded-[var(--radius-ctl)] px-2 py-1.5 text-sm text-[var(--text)] transition-colors hover:bg-[var(--surface-2)]"
        :title="t('usermenu.open')"
        :aria-label="t('usermenu.open')"
      >
        <span v-if="me">{{ me.username }}</span>
        <Settings class="size-4 text-[var(--text-2)]" />
      </button>
    </PopoverTrigger>
    <PopoverContent class="w-56 p-1.5" align="end">
      <!-- 身份头：用户名 + 角色徽章（只读） -->
      <div v-if="me" class="flex items-center justify-between gap-2 px-2 py-1.5">
        <span class="truncate text-sm font-medium">{{ me.username }}</span>
        <span class="shrink-0 rounded-full px-1.5 py-0.5 text-xs" :class="roleBadgeClass(me.role)">
          {{ roleText }}
        </span>
      </div>
      <div class="my-1 border-t border-[var(--border)]" />

      <!-- 语言：母语名平铺（复用 LanguagePicker inline 变体） -->
      <div class="flex items-center justify-between gap-2 px-2 py-1.5">
        <span class="text-sm text-[var(--text-3)]">{{ t("lang.label") }}</span>
        <LanguagePicker inline />
      </div>

      <!-- 主题切换 -->
      <button type="button" :class="ROW" @click="toggleTheme">
        <component :is="theme === 'dark' ? Sun : Moon" class="size-4 text-[var(--text-2)]" />
        {{ t(theme === "dark" ? "portal.topbar.to_light" : "portal.topbar.to_dark") }}
      </button>

      <!-- 修改密码（仅登录账号会话有意义） -->
      <button v-if="me" type="button" :class="ROW" @click="act('changePassword')">
        <KeyRound class="size-4 text-[var(--text-2)]" />
        {{ t("portal.topbar.change_pw") }}
      </button>

      <div class="my-1 border-t border-[var(--border)]" />
      <button
        type="button"
        :class="ROW + ' text-[var(--err)]'"
        @click="act('logout')"
      >
        <LogOut class="size-4" />
        {{ t("portal.topbar.logout") }}
      </button>
    </PopoverContent>
  </Popover>
</template>
