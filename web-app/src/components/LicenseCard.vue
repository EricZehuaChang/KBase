<script setup lang="ts">
// 许可证状态卡片（设置页，仅 admin 可见）：状态徽章 + org/expires（存在时）。
// 不锁功能（spec §6），纯展示；AppShell 顶部横幅（用 licenseBannerInfo）是
// 同一份数据在全局层面的提示，这里是设置页内的详情视图。
import { onMounted, ref } from "vue";
import { useI18n } from "vue-i18n";
import { toast } from "vue-sonner";
import { Badge } from "@/components/ui/badge";
import { getLicense, type LicenseInfo } from "@/lib/api";

const { t } = useI18n();

const STATUS_BADGE_CLASS: Record<string, string> = {
  trial: "bg-[var(--accent-weak)] text-[var(--accent-text)]",
  valid: "bg-[var(--ok-weak)] text-[var(--ok)]",
  expired: "bg-[var(--warn-weak)] text-[var(--warn)]",
  invalid: "bg-[var(--err-weak)] text-[var(--err)]",
};

const license = ref<LicenseInfo | null>(null);

onMounted(async () => {
  try {
    license.value = await getLicense();
  } catch (err) {
    toast.error(err instanceof Error ? err.message : String(err));
  }
});
</script>

<template>
  <section class="rounded-[var(--radius-card)] border border-[var(--border)] bg-[var(--surface)] p-4">
    <h2 class="mb-3 text-sm font-medium text-[var(--text-2)]">{{ t("license.title") }}</h2>
    <div v-if="license" class="flex flex-wrap items-center gap-3 text-sm">
      <Badge :class="STATUS_BADGE_CLASS[license.status]">{{ t(`license.status.${license.status}`) }}</Badge>
      <span v-if="license.org" class="text-[var(--text-2)]">{{ t("license.org", { org: license.org }) }}</span>
      <span v-if="license.expires" class="text-[var(--text-2)]">{{ t("license.expires", { date: license.expires }) }}</span>
    </div>
    <p v-else class="text-sm text-[var(--text-3)]">{{ t("common.loading") }}</p>
  </section>
</template>
