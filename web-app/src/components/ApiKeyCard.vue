<script setup lang="ts">
// API Key 卡片（设置页，仅 admin 可见）：列表（prefix/name/role/revoked）；
// 新建/吊销两个 Dialog 拆到 ApiKeyFormDialogs.vue（>200 行拆分约定）。
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
          <TableHead class="w-24">{{ t("common.actions") }}</TableHead>
        </TableRow>
      </TableHeader>
      <TableBody>
        <TableEmpty v-if="!loading && keys.length === 0" :colspan="5">{{ t("apikey.empty") }}</TableEmpty>
        <TableRow v-for="k in keys" :key="k.id">
          <TableCell>{{ k.name }}</TableCell>
          <TableCell class="font-mono text-xs text-[var(--text-3)]">{{ k.prefix }}</TableCell>
          <TableCell>{{ t(`common.role.${k.role}`) }}</TableCell>
          <TableCell>
            <Badge :class="k.revoked ? 'bg-[var(--err-weak)] text-[var(--err)]' : 'bg-[var(--ok-weak)] text-[var(--ok)]'">
              {{ k.revoked ? t("apikey.revoked") : t("apikey.valid") }}
            </Badge>
          </TableCell>
          <TableCell>
            <Button v-if="!k.revoked" variant="ghost" size="sm" @click="revokeTarget = k">
              {{ t("apikey.revoke") }}
            </Button>
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
</template>
