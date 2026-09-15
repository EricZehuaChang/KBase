<script setup lang="ts">
// 渠道身份映射（T19）：把外部渠道账号（飞书 open_id）绑到 KBase 用户。
//
// 这张表解决的是"群里提问的人在 KBase 里是谁"：没有映射时渠道入口只能用一个
// 笼统身份查库，"谁能问什么"无从表达；映射之后同一句提问由不同的人发出会拿到
// 不同结果（有权的人查得到、无权的人被静默拒答），与登录态问答同一套库级权限。
//
// 未映射的人**不是全放行**：按匿名 viewer 处理——公开库能问、收紧过的库问不到。
// 解绑/停用/删除用户都立刻回落这条默认策略（后端每次问答现查，不缓存）。
import { onMounted, ref } from "vue";
import { useI18n } from "vue-i18n";
import { Loader2, Plus, Trash2 } from "@lucide/vue";
import { Badge } from "@/components/ui/badge";
import { Button } from "@/components/ui/button";
import { toast } from "vue-sonner";
import {
  bindChannelIdentity, listChannelIdentities, listChannels, listUsers,
  unbindChannelIdentity,
  type ChannelIdentity, type ChannelOption, type UserItem,
} from "@/lib/api";

const { t } = useI18n();

const channels = ref<ChannelOption[]>([]);
const identities = ref<ChannelIdentity[]>([]);
const users = ref<UserItem[]>([]);
const loading = ref(true);
const saving = ref(false);
const error = ref<string | null>(null);

const channel = ref("");
const externalId = ref("");
const userId = ref("");

/** UTC naive ISO（后端写的是 utcnow）→ 本地可读时间；解析失败原样显示。 */
function formatTime(iso: string): string {
  const d = new Date(iso.endsWith("Z") ? iso : `${iso}Z`);
  return Number.isNaN(d.getTime()) ? iso : d.toLocaleString();
}

async function load(): Promise<void> {
  try {
    const [ch, ids, us] = await Promise.all([
      listChannels(), listChannelIdentities(), listUsers()]);
    channels.value = ch.items;
    identities.value = ids.items;
    users.value = us;
    if (!channel.value && ch.items.length) channel.value = ch.items[0].channel;
  } catch (err) {
    error.value = err instanceof Error ? err.message : String(err);
  } finally {
    loading.value = false;
  }
}

async function submit(): Promise<void> {
  if (!channel.value || !externalId.value.trim() || !userId.value) return;
  saving.value = true;
  try {
    await bindChannelIdentity({
      channel: channel.value,
      external_user_id: externalId.value.trim(),
      user_id: userId.value,
    });
    externalId.value = "";
    toast.success(t("channel.bound"));
    await load();
  } catch (err) {
    toast.error(err instanceof Error ? err.message : String(err));
  } finally {
    saving.value = false;
  }
}

async function remove(row: ChannelIdentity): Promise<void> {
  try {
    await unbindChannelIdentity(row.id);
    toast.success(t("channel.unbound"));
    await load();
  } catch (err) {
    toast.error(err instanceof Error ? err.message : String(err));
  }
}

onMounted(load);
</script>

<template>
  <article class="rounded-[var(--radius-card)] border border-[var(--border)] bg-[var(--surface)] p-4">
    <div class="mb-1 font-medium">{{ t("channel.title") }}</div>
    <p class="mb-3 text-xs text-[var(--text-3)]">{{ t("channel.desc") }}</p>

    <p v-if="error" class="text-sm text-[var(--err)]">⚠️ {{ error }}</p>
    <div v-else-if="loading" class="flex items-center gap-2 py-3 text-sm text-[var(--text-3)]">
      <Loader2 class="size-4 animate-spin" />{{ t("common.loading") }}
    </div>
    <template v-else>
      <!-- 新增/改绑：同一渠道内同一个外部账号再提交就是改绑（覆盖，不留双份） -->
      <div class="mb-4 flex flex-wrap items-end gap-2">
        <label class="flex flex-col gap-1 text-xs text-[var(--text-3)]">
          {{ t("channel.channel") }}
          <select
            v-model="channel"
            class="rounded-[var(--radius-input)] border border-[var(--border)] bg-[var(--surface)] px-2 py-1.5 text-sm text-[var(--text-2)]"
          >
            <option v-for="c in channels" :key="c.channel" :value="c.channel">
              {{ c.label }}
            </option>
          </select>
        </label>
        <label class="flex min-w-[14rem] flex-1 flex-col gap-1 text-xs text-[var(--text-3)]">
          {{ t("channel.external_id") }}
          <input
            v-model="externalId"
            class="rounded-[var(--radius-input)] border border-[var(--border)] bg-[var(--surface)] px-2 py-1.5 font-mono text-sm text-[var(--text-2)]"
            :placeholder="t('channel.external_id_ph')"
            @keyup.enter="submit"
          >
        </label>
        <label class="flex min-w-[12rem] flex-col gap-1 text-xs text-[var(--text-3)]">
          {{ t("channel.user") }}
          <select
            v-model="userId"
            class="rounded-[var(--radius-input)] border border-[var(--border)] bg-[var(--surface)] px-2 py-1.5 text-sm text-[var(--text-2)]"
          >
            <option value="">{{ t("channel.user_ph") }}</option>
            <option v-for="u in users" :key="u.id" :value="u.id">
              {{ u.username }}（{{ u.role }}）
            </option>
          </select>
        </label>
        <Button
          size="sm"
          :disabled="saving || !channel || !externalId.trim() || !userId"
          @click="submit"
        >
          <Loader2 v-if="saving" class="size-3.5 animate-spin" />
          <Plus v-else class="size-3.5" />
          {{ t("channel.bind") }}
        </Button>
      </div>

      <p v-if="!identities.length" class="text-sm text-[var(--text-3)]">
        {{ t("channel.empty") }}
      </p>
      <ul v-else class="flex flex-col gap-1.5">
        <li
          v-for="row in identities"
          :key="row.id"
          class="flex flex-wrap items-center gap-2 text-sm"
        >
          <Badge class="bg-[var(--surface-2)] text-[var(--text-2)]">
            {{ channels.find((c) => c.channel === row.channel)?.label ?? row.channel }}
          </Badge>
          <span class="truncate font-mono text-xs text-[var(--text-2)]">
            {{ row.external_user_id }}
          </span>
          <span class="text-[var(--text-3)]">→</span>
          <!-- 用户被删后绑定行还在（历史绑定）：显示成缺失态提示管理员清理，
               不显示空白（空白会让人以为这行是脏数据） -->
          <span v-if="row.username" class="text-[var(--text-2)]">
            {{ row.username }}
            <span class="text-xs text-[var(--text-3)]">（{{ row.role }}）</span>
            <span v-if="row.disabled" class="text-xs text-[var(--warn)]">
              · {{ t("channel.disabled") }}
            </span>
          </span>
          <span v-else class="text-xs text-[var(--err)]">{{ t("channel.user_missing") }}</span>
          <span class="ml-auto shrink-0 text-xs text-[var(--text-3)]">
            {{ formatTime(row.created_at) }}
          </span>
          <Button variant="ghost" size="sm" @click="remove(row)">
            <Trash2 class="size-3.5" />
            {{ t("channel.unbind") }}
          </Button>
        </li>
      </ul>
    </template>
  </article>
</template>
