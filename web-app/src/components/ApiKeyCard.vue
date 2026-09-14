<script setup lang="ts">
// API Key 卡片（设置页，仅 admin 可见）：列表（name/prefix/role/状态/限制/
// 最近使用）+ 行操作（编辑/用量/吊销）；新建/吊销与编辑、用量 Dialog 各自
// 拆到独立组件（>200 行拆分约定）。
// T09：状态区分"吊销（不可恢复）/停用（可恢复）/过期/有效"，并展示配额与
// 库白名单范围——此前列表里看不到任何限制信息。
import { onMounted, ref } from "vue";
import { useI18n } from "vue-i18n";
import { toast } from "vue-sonner";
import { Plus } from "@lucide/vue";
import { Button } from "@/components/ui/button";
import { Badge } from "@/components/ui/badge";
import {
  Table, TableHeader, TableBody, TableRow, TableHead, TableCell, TableEmpty,
} from "@/components/ui/table";
import ApiKeyFormDialogs from "@/components/ApiKeyFormDialogs.vue";
import ApiKeyEditDialog from "@/components/ApiKeyEditDialog.vue";
import ApiKeyUsageDialog from "@/components/ApiKeyUsageDialog.vue";
import { apiKeyState, formatKeyTime, type ApiKeyState } from "@/lib/settings-utils";
import { listApiKeys, type ApiKeyItem } from "@/lib/api";

const { t } = useI18n();

const keys = ref<ApiKeyItem[]>([]);
const loading = ref(true);

async function load() {
  loading.value = true;
  try {
    keys.value = await listApiKeys();
  } catch (err) {
    toast.error(err instanceof Error ? err.message : String(err));
  } finally {
    loading.value = false;
  }
}

onMounted(load);

const createOpen = ref(false);
const revokeTarget = ref<ApiKeyItem | null>(null);
const editTarget = ref<ApiKeyItem | null>(null);
const usageTarget = ref<ApiKeyItem | null>(null);

// 状态徽章配色：吊销/过期=红，停用=黄，有效=绿（与状态裁决同一优先级）
const BADGE: Record<ApiKeyState, string> = {
  revoked: "bg-[var(--err-weak)] text-[var(--err)]",
  expired: "bg-[var(--err-weak)] text-[var(--err)]",
  disabled: "bg-[var(--warn-weak)] text-[var(--warn)]",
  valid: "bg-[var(--ok-weak)] text-[var(--ok)]",
};

function stateOf(k: ApiKeyItem): ApiKeyState {
  return apiKeyState(k);
}

/** 限制摘要：每分钟/每日配额 + 库范围 + IP 白名单条数（不限的一律显式写"不限"，
 * 空着会让人以为是没配好）。 */
function limitsOf(k: ApiKeyItem): string[] {
  const parts: string[] = [];
  if (k.rpm != null || k.daily_quota != null) {
    const rpm = k.rpm == null ? t("apikey.unlimited") : `${k.rpm}${t("apikey.per_min")}`;
    const quota = k.daily_quota == null ? t("apikey.unlimited") : `${k.daily_quota}${t("apikey.per_day")}`;
    parts.push(`${rpm} / ${quota}`);
  }
  parts.push(k.scope_kb_ids?.length
    ? t("apikey.scope_n", { n: k.scope_kb_ids.length }) : t("apikey.scope_all"));
  if (k.ip_allow?.length) parts.push(t("apikey.ip_n", { n: k.ip_allow.length }));
  return parts;
}

function lastUsedText(k: ApiKeyItem): string {
  return formatKeyTime(k.last_used_at) ?? t("apikey.never_used");
}
</script>

<template>
  <section class="rounded-[var(--radius-card)] border border-[var(--border)] bg-[var(--surface)] p-4">
    <div class="mb-3 flex items-center justify-between">
      <h2 class="text-sm font-medium text-[var(--text-2)]">API Key</h2>
      <Button size="sm" @click="createOpen = true">
        <Plus class="size-3.5" />
        {{ t("apikey.create") }}
      </Button>
    </div>

    <Table>
      <TableHeader>
        <TableRow>
          <TableHead>{{ t("common.name") }}</TableHead>
          <TableHead>{{ t("apikey.prefix") }}</TableHead>
          <TableHead>{{ t("common.role_col") }}</TableHead>
          <TableHead>{{ t("common.status") }}</TableHead>
          <TableHead>{{ t("apikey.limits") }}</TableHead>
          <TableHead>{{ t("apikey.last_used") }}</TableHead>
          <TableHead class="w-48">{{ t("common.actions") }}</TableHead>
        </TableRow>
      </TableHeader>
      <TableBody>
        <TableEmpty v-if="!loading && keys.length === 0" :colspan="7">{{ t("apikey.empty") }}</TableEmpty>
        <TableRow v-for="k in keys" :key="k.id">
          <TableCell>{{ k.name }}</TableCell>
          <TableCell class="font-mono text-xs text-[var(--text-3)]">{{ k.prefix }}</TableCell>
          <TableCell>{{ t(`common.role.${k.role}`) }}</TableCell>
          <TableCell>
            <Badge :class="BADGE[stateOf(k)]">
              {{ t(`apikey.state.${stateOf(k)}`) }}
            </Badge>
          </TableCell>
          <TableCell class="text-xs text-[var(--text-3)]">
            <span v-for="p in limitsOf(k)" :key="p" class="mr-2 inline-block">{{ p }}</span>
          </TableCell>
          <TableCell class="text-xs text-[var(--text-3)]">{{ lastUsedText(k) }}</TableCell>
          <TableCell>
            <div class="flex items-center gap-1">
              <Button variant="ghost" size="sm" @click="editTarget = k">
                {{ t("apikey.edit") }}
              </Button>
              <Button variant="ghost" size="sm" @click="usageTarget = k">
                {{ t("apikey.usage") }}
              </Button>
              <!-- 吊销=软删除且置 revoked，行仍在列表中，故只对未吊销行提供 -->
              <Button v-if="!k.revoked" variant="ghost" size="sm" @click="revokeTarget = k">
                {{ t("apikey.revoke") }}
              </Button>
            </div>
          </TableCell>
        </TableRow>
      </TableBody>
    </Table>
  </section>

  <ApiKeyFormDialogs
    v-model:create-open="createOpen"
    v-model:revoke-target="revokeTarget"
    @changed="load"
  />
  <ApiKeyEditDialog
    :target="editTarget"
    @update:target="(v) => { editTarget = v; }"
    @changed="load"
  />
  <ApiKeyUsageDialog
    :target="usageTarget"
    @update:target="(v) => { usageTarget = v; }"
  />
</template>
