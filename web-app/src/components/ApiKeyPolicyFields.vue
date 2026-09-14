<script setup lang="ts">
// API Key 策略字段（T09）：有效期 / 每分钟与每日配额 / 来源 IP 白名单。
// 新建（ApiKeyFormDialogs）与编辑（ApiKeyEditDialog）两个表单共用同一份
// 输入与提示文案——两处各写一份，改一处漏一处必然口径漂移。
// 四个 model 都是字符串（空串=该维度不限），转换见 settings-utils 的
// buildApiKeyPolicy（含"空输入即清除限制"的规则）。
import { useI18n } from "vue-i18n";
import { Input } from "@/components/ui/input";

const { t } = useI18n();

const expiresAt = defineModel<string>("expiresAt", { required: true });
const rpm = defineModel<string>("rpm", { required: true });
const dailyQuota = defineModel<string>("dailyQuota", { required: true });
const ipAllow = defineModel<string>("ipAllow", { required: true });
</script>

<template>
  <div class="flex flex-col gap-3">
    <div class="grid grid-cols-2 gap-3">
      <label class="flex flex-col gap-1">
        <span class="text-sm text-[var(--text-2)]">{{ t("apikey.expires") }}</span>
        <Input v-model="expiresAt" type="date" />
      </label>
      <label class="flex flex-col gap-1">
        <span class="text-sm text-[var(--text-2)]">{{ t("apikey.rpm") }}</span>
        <Input v-model="rpm" type="number" min="1" :placeholder="t('apikey.unlimited')" />
      </label>
      <label class="flex flex-col gap-1">
        <span class="text-sm text-[var(--text-2)]">{{ t("apikey.daily_quota") }}</span>
        <Input v-model="dailyQuota" type="number" min="1" :placeholder="t('apikey.unlimited')" />
      </label>
    </div>
    <label class="flex flex-col gap-1">
      <span class="text-sm text-[var(--text-2)]">{{ t("apikey.ip_allow") }}</span>
      <textarea
        v-model="ipAllow"
        rows="2"
        :placeholder="t('apikey.ip_allow_ph')"
        class="rounded-[var(--radius-ctl)] border border-[var(--border)] bg-[var(--surface)] px-3 py-2 font-mono text-sm text-[var(--text)] outline-none focus:border-[var(--accent)]"
      />
      <span class="text-xs text-[var(--text-3)]">{{ t("apikey.ip_allow_hint") }}</span>
    </label>
    <p class="text-xs text-[var(--text-3)]">{{ t("apikey.quota_hint") }}</p>
  </div>
</template>
