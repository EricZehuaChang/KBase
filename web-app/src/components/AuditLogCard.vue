<script setup lang="ts">
// 审计日志卡片（设置页·运营看板 tab，admin 及以上）：活动趋势条形图 +
// 筛选表格 + 分页加载。数据源 GET /api/audit——后端已按查看者分层（超管
// 看全量，普通 admin 的视图里不出现超管 actor 的行，total 同口径），本卡
// 只管展示不做权限判断。趋势图/今日数基于已加载的行（每页 200，可续载），
// 图注如实标注口径，不冒充全量统计。
import { computed, onMounted, ref } from "vue";
import { useI18n } from "vue-i18n";
import { Button } from "@/components/ui/button";
import { Input } from "@/components/ui/input";
import {
  Select, SelectContent, SelectGroup, SelectItem, SelectTrigger, SelectValue,
} from "@/components/ui/select";
import { listAudit, getSession, type AuditItem } from "@/lib/api";
import { auditDailyCounts, parseAuditTs } from "@/lib/settings-utils";

const { t } = useI18n();

const PAGE = 200;
const items = ref<AuditItem[]>([]);
const total = ref(0);
const loading = ref(false);
const error = ref<string | null>(null);
const myName = ref<string | null>(null);

// 筛选（客户端，作用于已加载行）："__all__" 哨兵=不筛（Select 不收空串）
const actorFilter = ref("__all__");
const actionFilter = ref("");

async function load(offset = 0) {
  loading.value = true;
  try {
    const page = await listAudit(PAGE, offset);
    items.value = offset === 0 ? page.items : [...items.value, ...page.items];
    total.value = page.total;
    error.value = null;
  } catch (err) {
    error.value = err instanceof Error ? err.message : String(err);
  } finally {
    loading.value = false;
  }
}

onMounted(async () => {
  await load();
  try {
    myName.value = (await getSession())?.username ?? null;
  } catch { /* 拿不到用户名只是"只看我"不可用，不阻塞展示 */ }
});

const actors = computed(() =>
  [...new Set(items.value.map((i) => i.actor))].sort());

const filtered = computed(() => items.value.filter((i) =>
  (actorFilter.value === "__all__" || i.actor === actorFilter.value)
  && (!actionFilter.value.trim()
      || i.action.toLowerCase().includes(actionFilter.value.trim().toLowerCase()))));

// 趋势图（近 14 天，基于已加载行）；今日数取最后一格
const daily = computed(() => auditDailyCounts(items.value, 14));
const maxDaily = computed(() =>
  Math.max(1, ...daily.value.map((d) => d.count)));
const todayCount = computed(() =>
  daily.value[daily.value.length - 1]?.count ?? 0);

function fmtTs(ts: string): string {
  const d = parseAuditTs(ts);
  const p = (n: number) => String(n).padStart(2, "0");
  return `${p(d.getMonth() + 1)}-${p(d.getDate())} ${p(d.getHours())}:${p(d.getMinutes())}`;
}

function toggleOnlyMe() {
  if (!myName.value) return;
  actorFilter.value = actorFilter.value === myName.value ? "__all__" : myName.value;
}
</script>

<template>
  <article class="rounded-[var(--radius-card)] border border-[var(--border)] bg-[var(--surface)] p-4">
    <div class="mb-1 font-medium">{{ t("audit.title") }}</div>
    <p class="mb-3 text-xs text-[var(--text-3)]">{{ t("audit.desc") }}</p>
    <p v-if="error" class="text-sm text-[var(--err)]">⚠️ {{ error }}</p>

    <template v-else>
      <!-- 概览 chips -->
      <div class="mb-4 flex flex-wrap gap-6">
        <div>
          <div class="text-2xl font-semibold">{{ total }}</div>
          <div class="text-xs text-[var(--text-3)]">{{ t("audit.total") }}</div>
        </div>
        <div>
          <div class="text-2xl font-semibold">{{ todayCount }}</div>
          <div class="text-xs text-[var(--text-3)]">{{ t("audit.today") }}</div>
        </div>
        <div>
          <div class="text-2xl font-semibold">{{ items.length }}</div>
          <div class="text-xs text-[var(--text-3)]">{{ t("audit.loaded") }}</div>
        </div>
      </div>

      <!-- 近 14 天活动趋势（条形图，纯 div 零依赖） -->
      <div class="mb-1 text-sm font-medium text-[var(--text-2)]">
        {{ t("audit.trend_title") }}
      </div>
      <div class="mb-1 flex h-16 items-end gap-1">
        <div
          v-for="d in daily"
          :key="d.date"
          class="min-w-0 flex-1 rounded-t bg-[var(--accent)] transition-[height]"
          :class="d.count === 0 ? 'opacity-15' : 'opacity-80'"
          :style="{ height: `${Math.max(4, (d.count / maxDaily) * 100)}%` }"
          :title="`${d.date} · ${d.count}`"
        />
      </div>
      <p class="mb-4 text-xs text-[var(--text-3)]">
        {{ t("audit.trend_caption", { n: items.length }) }}
      </p>

      <!-- 筛选行 -->
      <div class="mb-2 flex flex-wrap items-center gap-2">
        <Select v-model="actorFilter">
          <SelectTrigger class="w-40"><SelectValue /></SelectTrigger>
          <SelectContent>
            <SelectGroup>
              <SelectItem value="__all__">{{ t("audit.all_actors") }}</SelectItem>
              <SelectItem v-for="a in actors" :key="a" :value="a">{{ a }}</SelectItem>
            </SelectGroup>
          </SelectContent>
        </Select>
        <Input
          v-model="actionFilter"
          class="w-44"
          :placeholder="t('audit.action_ph')"
        />
        <Button
          v-if="myName"
          size="sm"
          :variant="actorFilter === myName ? 'default' : 'outline'"
          @click="toggleOnlyMe"
        >
          {{ t("audit.only_me") }}
        </Button>
        <span class="text-xs text-[var(--text-3)]">
          {{ t("audit.showing", { shown: filtered.length, loaded: items.length }) }}
        </span>
      </div>

      <!-- 明细表 -->
      <p v-if="!filtered.length" class="py-2 text-sm text-[var(--text-3)]">
        {{ t("audit.empty") }}
      </p>
      <div v-else class="max-h-96 overflow-y-auto rounded-[var(--radius-ctl)] border border-[var(--border)]">
        <table class="w-full text-xs">
          <thead class="sticky top-0 bg-[var(--surface-2)] text-left text-[var(--text-3)]">
            <tr>
              <th class="px-2 py-1.5 font-medium">{{ t("audit.col_time") }}</th>
              <th class="px-2 py-1.5 font-medium">{{ t("audit.col_actor") }}</th>
              <th class="px-2 py-1.5 font-medium">{{ t("audit.col_action") }}</th>
              <th class="px-2 py-1.5 font-medium">{{ t("audit.col_resource") }}</th>
              <th class="px-2 py-1.5 font-medium">{{ t("audit.col_detail") }}</th>
              <th class="px-2 py-1.5 font-medium">IP</th>
            </tr>
          </thead>
          <tbody>
            <tr
              v-for="row in filtered"
              :key="row.id"
              class="border-t border-[var(--border)] align-top"
            >
              <td class="whitespace-nowrap px-2 py-1.5 text-[var(--text-3)]">{{ fmtTs(row.ts) }}</td>
              <td class="px-2 py-1.5">{{ row.actor }}</td>
              <td class="px-2 py-1.5"><code class="rounded bg-[var(--surface-2)] px-1">{{ row.action }}</code></td>
              <td class="max-w-40 truncate px-2 py-1.5 text-[var(--text-3)]" :title="row.resource ?? ''">{{ row.resource ?? "—" }}</td>
              <td class="max-w-56 truncate px-2 py-1.5 text-[var(--text-2)]" :title="row.detail ?? ''">{{ row.detail ?? "—" }}</td>
              <td class="whitespace-nowrap px-2 py-1.5 text-[var(--text-3)]">{{ row.ip ?? "—" }}</td>
            </tr>
          </tbody>
        </table>
      </div>

      <div v-if="items.length < total" class="mt-2">
        <Button size="sm" variant="outline" :disabled="loading" @click="load(items.length)">
          {{ loading ? t("common.loading") : t("audit.load_more", { rest: total - items.length }) }}
        </Button>
      </div>
    </template>
  </article>
</template>
