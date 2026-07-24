<script setup lang="ts">
// 顶栏语言切换器（portal + admin 复用）：地球图标弹出语言清单，选中即
// setLanguage（切 vue-i18n locale + 持久化 localStorage + <html lang> +
// 拉该语言 DB 覆盖）。清单从 i18n/languages 派生——加新语言零改本组件。
// inline 变体（登录页页脚）：不用悬浮图标+弹层——空页面角落孤零零一个地球
// 图标很突兀，且不认识当前界面语言的访客未必想到点它。平铺各语言的母语
// 自称文字（中文 · English · Bahasa Melayu），马来/英文客户第一眼就能看到
// 自己的语言名直接点。
import { computed } from "vue";
import { Check, Languages } from "@lucide/vue";
import { useI18n } from "vue-i18n";
import { Popover, PopoverContent, PopoverTrigger } from "@/components/ui/popover";
import { setLanguage } from "@/i18n";
import { LANGUAGES } from "@/i18n/languages";

defineProps<{ inline?: boolean }>();

const { t, locale } = useI18n();

// 顶栏触发钮带当前语言母语名（"文A 中文"/"文A English"）：光秃图标含义
// 不自明（用户反馈突兀），带名字读作一个有标签的控件
const currentName = computed(
  () => LANGUAGES.find((l) => l.code === locale.value)?.name ?? "");
</script>

<template>
  <!-- inline 变体：平铺母语名文字行（登录页页脚），当前语言高亮 -->
  <div
    v-if="inline"
    class="flex items-center gap-1 text-xs"
    role="group"
    :aria-label="t('lang.label')"
  >
    <template v-for="(l, i) in LANGUAGES" :key="l.code">
      <span v-if="i > 0" class="select-none text-[var(--text-3)]">·</span>
      <button
        type="button"
        class="rounded px-1.5 py-0.5 transition-colors"
        :class="locale === l.code
          ? 'font-medium text-[var(--accent-text)]'
          : 'text-[var(--text-3)] hover:text-[var(--text-2)]'"
        @click="setLanguage(l.code)"
      >{{ l.name }}</button>
    </template>
  </div>
  <Popover v-else>
    <PopoverTrigger as-child>
      <button
        type="button"
        class="flex items-center gap-1.5 rounded-[var(--radius-ctl)] px-2 py-1.5 text-[var(--text-2)] transition-colors hover:bg-[var(--surface-2)]"
        :title="t('lang.label')"
        :aria-label="t('lang.label')"
      >
        <Languages class="size-4" />
        <span class="text-xs">{{ currentName }}</span>
      </button>
    </PopoverTrigger>
    <PopoverContent class="w-44 p-1" align="end">
      <button
        v-for="l in LANGUAGES"
        :key="l.code"
        type="button"
        class="flex w-full items-center justify-between rounded-[var(--radius-ctl)] px-2 py-1.5 text-sm text-[var(--text)] transition-colors hover:bg-[var(--surface-2)]"
        @click="setLanguage(l.code)"
      >
        <span>{{ l.name }}</span>
        <Check v-if="locale === l.code" class="size-3.5 text-[var(--accent)]" />
      </button>
    </PopoverContent>
  </Popover>
</template>
