<script setup lang="ts">
// 登录页语言切换行：平铺各语言的母语自称文字（中文 · English · Bahasa
// Melayu），当前语言高亮，点击即 setLanguage（切 vue-i18n locale + 持久化
// localStorage + <html lang> + 拉该语言 DB 覆盖）。母语自称对不识当前界面
// 语言的访客一眼可认。清单从 i18n/languages 派生——加新语言零改本组件。
// 登录后的语言切换在顶栏用户菜单（UserMenu 二级列表），本组件只服务登录页。
import { useI18n } from "vue-i18n";
import { setLanguage } from "@/i18n";
import { LANGUAGES } from "@/i18n/languages";

const { t, locale } = useI18n();
</script>

<template>
  <div
    class="flex items-center gap-1 text-xs"
    role="group"
    :aria-label="t('lang.label')"
  >
    <template v-for="(l, i) in LANGUAGES" :key="l.code">
      <span v-if="i > 0" class="select-none text-[var(--text-3)]">·</span>
      <button
        type="button"
        class="whitespace-nowrap rounded px-1.5 py-0.5 transition-colors"
        :class="locale === l.code
          ? 'font-medium text-[var(--accent-text)]'
          : 'text-[var(--text-3)] hover:text-[var(--text-2)]'"
        @click="setLanguage(l.code)"
      >{{ l.name }}</button>
    </template>
  </div>
</template>
