<script setup lang="ts">
// 顶栏用户菜单（portal + admin 复用）：顶栏只保留登录人用户名 + 设置齿轮，
// 其余功能（角色徽章/语言/主题/修改密码/登出）全部收进下拉。
// 语言采用经典二级菜单模式（对标 YouTube/Google 账号菜单）：主行显示
// 「语言 — 当前语言 ›」，点入语言列表勾选（✓ 标当前项、各语言母语自称），
// 选中即时生效并返回主视图——主行立刻反映新语言，弹层不关，反馈可见。
// 改密对话框/登出逻辑留在 Shell（emit 上抛）；动作项点击即收起下拉。
import { computed, ref, watch } from "vue";
import { useI18n } from "vue-i18n";
import {
  Check, ChevronLeft, ChevronRight, KeyRound, Languages, LogOut, Moon,
  Play, Settings, Sun,
} from "@lucide/vue";
import { Popover, PopoverContent, PopoverTrigger } from "@/components/ui/popover";
import { setLanguage } from "@/i18n";
import { startTour } from "@/lib/demo-tour";
import { LANGUAGES } from "@/i18n/languages";
import { theme, toggleTheme } from "@/lib/theme";
import { roleBadgeClass } from "@/lib/auth-utils";
import type { Me } from "@/lib/api";

defineProps<{ me: Me | null; roleText: string }>();
const emit = defineEmits<{ changePassword: []; logout: [] }>();
const { t, locale } = useI18n();

const open = ref(false);
// 二级视图状态：main=主菜单；language=语言选择列表
const view = ref<"main" | "language">("main");
watch(open, () => { view.value = "main"; });   // 每次开合都回到主视图

const currentLangName = computed(
  () => LANGUAGES.find((l) => l.code === locale.value)?.name ?? "");

function pickLanguage(code: string) {
  void setLanguage(code);
  view.value = "main";    // 选完回主视图（弹层保持打开，主行即显新语言）
}

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
    <PopoverContent class="w-64 p-1.5" align="end">
      <!-- 主视图 -->
      <template v-if="view === 'main'">
        <!-- 身份头：用户名 + 角色徽章（只读） -->
        <div v-if="me" class="flex items-center justify-between gap-2 px-2 py-1.5">
          <span class="truncate text-sm font-medium">{{ me.username }}</span>
          <span class="shrink-0 rounded-full px-1.5 py-0.5 text-xs" :class="roleBadgeClass(me.role)">
            {{ roleText }}
          </span>
        </div>
        <div class="my-1 border-t border-[var(--border)]" />

        <!-- 语言主行：当前语言 + › 进入二级列表 -->
        <button type="button" :class="ROW" @click="view = 'language'">
          <Languages class="size-4 text-[var(--text-2)]" />
          <span class="flex-1 text-left">{{ t("lang.label") }}</span>
          <span class="text-xs text-[var(--text-3)]">{{ currentLangName }}</span>
          <ChevronRight class="size-3.5 text-[var(--text-3)]" />
        </button>

        <!-- 产品导览：仅白名单账号可见（me.tour_enabled，超管恒真）——
        演示者专属入口，普通同事不见 -->
        <button
          v-if="me?.tour_enabled"
          type="button" :class="ROW"
          @click="open = false; startTour()"
        >
          <Play class="size-4 text-[var(--text-2)]" />
          {{ t("tour.ui.open") }}
        </button>

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
      </template>

      <!-- 语言二级视图：‹ 返回头 + 母语名列表（✓ 标当前） -->
      <template v-else>
        <button type="button" :class="ROW + ' font-medium'" @click="view = 'main'">
          <ChevronLeft class="size-4 text-[var(--text-2)]" />
          {{ t("lang.label") }}
        </button>
        <div class="my-1 border-t border-[var(--border)]" />
        <button
          v-for="l in LANGUAGES"
          :key="l.code"
          type="button"
          :class="ROW + ' justify-between'"
          @click="pickLanguage(l.code)"
        >
          <span>{{ l.name }}</span>
          <Check v-if="locale === l.code" class="size-3.5 text-[var(--accent)]" />
        </button>
      </template>
    </PopoverContent>
  </Popover>
</template>
