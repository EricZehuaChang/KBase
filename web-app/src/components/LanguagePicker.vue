<script setup lang="ts">
// 顶栏语言切换器（portal + admin 复用）：地球图标弹出语言清单，选中即
// setLanguage（切 vue-i18n locale + 持久化 localStorage + <html lang> +
// 拉该语言 DB 覆盖）。清单从 i18n/languages 派生——加新语言零改本组件。
import { Check, Languages } from "@lucide/vue";
import { useI18n } from "vue-i18n";
import { Popover, PopoverContent, PopoverTrigger } from "@/components/ui/popover";
import { setLanguage } from "@/i18n";
import { LANGUAGES } from "@/i18n/languages";

const { t, locale } = useI18n();
</script>

<template>
  <Popover>
    <PopoverTrigger as-child>
      <button
        type="button"
        class="rounded-[var(--radius-ctl)] p-2 text-[var(--text-2)] transition-colors hover:bg-[var(--surface-2)]"
        :title="t('lang.label')"
        :aria-label="t('lang.label')"
      >
        <Languages class="size-4" />
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
