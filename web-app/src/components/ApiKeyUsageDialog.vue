<script setup lang="ts">
// API Key 用量对话框（T09）：近 N 天逐日请求数 + token 数。
// 口径提示写在弹窗里（token 为按字符数估算、日按 UTC 切分、per-key 数据
// 只在管理端可见）——运维手册 §3.1 是同一套口径，两边别各说一套。
import { computed, ref, watch } from "vue";
import { useI18n } from "vue-i18n";
import { toast } from "vue-sonner";
import {
  Dialog, DialogContent, DialogHeader, DialogTitle, DialogDescription,
} from "@/components/ui/dialog";
import {
  Table, TableHeader, TableBody, TableRow, TableHead, TableCell, TableEmpty,
} from "@/components/ui/table";
import { getApiKeyUsage, type ApiKeyItem, type ApiKeyUsage } from "@/lib/api";

const props = defineProps<{ target: ApiKeyItem | null }>();
const emit = defineEmits<{ "update:target": [value: ApiKeyItem | null] }>();
const { t } = useI18n();

const DAYS = 30;
const usage = ref<ApiKeyUsage | null>(null);
const loading = ref(false);

// 倒序展示（最近的日期在最上面）：运维看用量先看今天
const rows = computed(() => [...(usage.value?.items ?? [])].reverse());

function limitText(): string {
  const u = usage.value;
  if (!u) return t("apikey.unlimited");
  const rpm = u.rpm == null ? t("apikey.unlimited") : String(u.rpm);
  const quota = u.daily_quota == null ? t("apikey.unlimited") : String(u.daily_quota);
  return `${t("apikey.rpm")} ${rpm} · ${t("apikey.daily_quota")} ${quota}`;
}

watch(() => props.target, async (k) => {
  if (!k) return;
  loading.value = true;
  usage.value = null;
  try {
    usage.value = await getApiKeyUsage(k.id, DAYS);
  } catch (err) {
    toast.error(err instanceof Error ? err.message : String(err));
  } finally {
    loading.value = false;
  }
});
</script>

<template>
  <Dialog :open="!!target" @update:open="(v) => { if (!v) emit('update:target', null); }">
    <DialogContent class="max-h-[85vh] max-w-2xl overflow-hidden">
      <DialogHeader>
        <DialogTitle>{{ t("apikey.usage_title", { name: target?.name }) }}</DialogTitle>
        <DialogDescription>{{ t("apikey.usage_desc", { days: DAYS }) }}</DialogDescription>
      </DialogHeader>

      <p v-if="target" class="text-xs text-[var(--text-3)]">{{ t("apikey.limits") }}: {{ limitText() }}</p>

      <div class="max-h-[55vh] overflow-y-auto">
        <p v-if="loading" class="py-4 text-center text-sm text-[var(--text-3)]">{{ t("common.loading") }}</p>
        <Table v-else>
          <TableHeader>
            <TableRow>
              <TableHead>{{ t("apikey.usage_day") }}</TableHead>
              <TableHead>{{ t("apikey.usage_requests") }}</TableHead>
              <TableHead>{{ t("apikey.usage_prompt") }}</TableHead>
              <TableHead>{{ t("apikey.usage_completion") }}</TableHead>
            </TableRow>
          </TableHeader>
          <TableBody>
            <TableEmpty v-if="!rows.length" :colspan="4">{{ t("apikey.usage_empty") }}</TableEmpty>
            <TableRow v-for="r in rows" :key="r.day">
              <TableCell class="font-mono text-xs">{{ r.day }}</TableCell>
              <TableCell>{{ r.requests }}</TableCell>
              <TableCell>{{ r.prompt_tokens }}</TableCell>
              <TableCell>{{ r.completion_tokens }}</TableCell>
            </TableRow>
            <TableRow v-if="rows.length && usage">
              <TableCell class="text-[var(--text-2)]">{{ t("apikey.usage_total") }}</TableCell>
              <TableCell>{{ usage.totals.requests }}</TableCell>
              <TableCell>{{ usage.totals.prompt_tokens }}</TableCell>
              <TableCell>{{ usage.totals.completion_tokens }}</TableCell>
            </TableRow>
          </TableBody>
        </Table>
      </div>

      <!-- 口径如实标注：估算值/UTC 日/仅管理端可见（运维手册 §3.1 同一口径） -->
      <p class="text-xs text-[var(--text-3)]">{{ t("apikey.usage_note") }}</p>
    </DialogContent>
  </Dialog>
</template>
