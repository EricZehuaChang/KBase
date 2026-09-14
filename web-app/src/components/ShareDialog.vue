<script setup lang="ts">
// 分享链接管理对话框（KB 详情页，editor+）：建链接（备注+绑定回答模型，
// 对标 Dify/FastGPT——模型是建链接者的决策，终端用户无感）、复制链接/
// 嵌入代码、撤销。撤销立即生效（公开端点 404）。
// T10：可加有效期/访问口令/次数上限（留空=该维度不限），列表回显用量。
import { onMounted, ref, watch } from "vue";
import { useI18n } from "vue-i18n";
import { toast } from "vue-sonner";
import { Copy, Trash2 } from "@lucide/vue";
import { copyToClipboard } from "@/lib/clipboard";
import { Button } from "@/components/ui/button";
import { Input } from "@/components/ui/input";
import {
  Dialog, DialogContent, DialogHeader, DialogTitle, DialogDescription,
} from "@/components/ui/dialog";
import {
  Select, SelectContent, SelectGroup, SelectItem, SelectTrigger, SelectValue,
} from "@/components/ui/select";
import {
  createShareLink, listShareLinks, revokeShareLink, listProviders, listKbs,
  type ShareLinkItem, type Kb,
} from "@/lib/api";

const props = defineProps<{ kbId: string }>();
const open = defineModel<boolean>("open", { required: true });
const { t } = useI18n();

const links = ref<ShareLinkItem[]>([]);
const providers = ref<string[]>([]);
const name = ref("");
// "__default__" 哨兵：Select 组件不接受空串 value，用哨兵表达"系统默认"
const provider = ref("__default__");
const busy = ref(false);
// 多库联查（对标登录端 + 联查）：勾选的副库随建链接一并绑定，匿名问答
// 跨全部库散射检索。清单排除当前主库自身。
const otherKbs = ref<Kb[]>([]);
const extraKbIds = ref<string[]>([]);
// T10 策略输入：三个都是字符串，空串=该维度不限（与 T09 ApiKey 策略表单
// 同一套约定）。有效期只发日期，后端按当日 23:59:59 收口。
const expiresAt = ref("");
const password = ref("");
const maxVisits = ref("");

async function refresh() {
  try {
    links.value = await listShareLinks(props.kbId);
  } catch (err) {
    toast.error(err instanceof Error ? err.message : String(err));
  }
}

onMounted(async () => {
  try {
    providers.value = (await listProviders()).providers;
  } catch {
    // provider 清单拉不到不阻塞建链接（用系统默认）
  }
  try {
    otherKbs.value = (await listKbs()).filter((kb) => kb.id !== props.kbId);
  } catch {
    // 库清单拉不到只是没有联查选项，单库建链接照常
  }
});
watch(open, (v) => { if (v) refresh(); }, { immediate: true });

function toggleExtra(id: string) {
  extraKbIds.value = extraKbIds.value.includes(id)
    ? extraKbIds.value.filter((x) => x !== id)
    : [...extraKbIds.value, id];
}

async function create() {
  busy.value = true;
  try {
    await createShareLink(props.kbId, {
      name: name.value.trim(),
      provider: provider.value === "__default__" ? null : provider.value,
      extra_kb_ids: extraKbIds.value,
      // 空串一律转 null=不限：后端 NULL 语义（老链接与"不填"是同一条路径）
      expires_at: expiresAt.value.trim() || null,
      password: password.value.trim() || null,
      max_visits: maxVisits.value.trim() ? Number(maxVisits.value.trim()) : null,
    });
    name.value = "";
    extraKbIds.value = [];
    expiresAt.value = "";
    password.value = "";      // 口令不回显（后端只存哈希，也从不回传）
    maxVisits.value = "";
    toast.success(t("sharedlg.created"));
    await refresh();
  } catch (err) {
    toast.error(err instanceof Error ? err.message : String(err));
  } finally {
    busy.value = false;
  }
}

function shareUrl(link: ShareLinkItem): string {
  return `${window.location.origin}/share/${link.token}`;
}

function embedSnippet(link: ShareLinkItem): string {
  return `<script src="${window.location.origin}/widget.js" `
    + `data-kbase-share="${link.token}" defer><\/script>`;
}

/** 有效期展示：后端存 naive UTC ISO，取前 10 位即所选日期（同 T09 ApiKey）。 */
function expiryText(link: ShareLinkItem): string | null {
  return link.expires_at ? link.expires_at.slice(0, 10) : null;
}

/** 用量展示：有上限显示 n/max，无上限只显示已访问次数（0 次不占版面）。 */
function visitText(link: ShareLinkItem): string | null {
  const n = link.visit_count ?? 0;
  if (link.max_visits == null) {
    return n > 0 ? t("sharedlg.badge_visits", { n }) : null;
  }
  return t("sharedlg.badge_visits_capped", { n, max: link.max_visits });
}

async function copy(text: string, label: string) {
  // 走兼容工具：http 演示机（非安全上下文）拿不到 navigator.clipboard，
  // 直接调用会静默失败（真机踩过），copyToClipboard 内含 execCommand 回退。
  if (await copyToClipboard(text)) toast.success(t("sharedlg.copied", { label }));
  else toast.error(t("msg.copy_failed"));
}

async function revoke(link: ShareLinkItem) {
  try {
    await revokeShareLink(link.id);
    toast.success(t("sharedlg.revoked"));
    await refresh();
  } catch (err) {
    toast.error(err instanceof Error ? err.message : String(err));
  }
}
</script>

<template>
  <Dialog v-model:open="open">
    <DialogContent class="sm:max-w-[560px]">
      <DialogHeader>
        <DialogTitle>{{ t("sharedlg.title") }}</DialogTitle>
        <DialogDescription>
          {{ t("sharedlg.desc") }}
        </DialogDescription>
      </DialogHeader>

      <!-- 建链接 -->
      <div class="flex items-end gap-2">
        <label class="flex flex-1 flex-col gap-1">
          <span class="text-sm text-[var(--text-2)]">{{ t("sharedlg.note") }}</span>
          <Input v-model="name" :placeholder="t('sharedlg.note_ph')" @keydown.enter="create" />
        </label>
        <label class="flex w-40 flex-col gap-1">
          <span class="text-sm text-[var(--text-2)]">{{ t("sharedlg.model") }}</span>
          <Select v-model="provider">
            <SelectTrigger><SelectValue /></SelectTrigger>
            <SelectContent>
              <SelectGroup>
                <SelectItem value="__default__">{{ t("sharedlg.default_model") }}</SelectItem>
                <SelectItem v-for="p in providers" :key="p" :value="p">{{ p }}</SelectItem>
              </SelectGroup>
            </SelectContent>
          </Select>
        </label>
        <Button :disabled="busy" @click="create">{{ t("common.create") }}</Button>
      </div>

      <!-- T10 访问策略（可选）：有效期/口令/次数上限，留空=该维度不限 -->
      <div class="flex flex-col gap-1.5">
        <span class="text-sm text-[var(--text-2)]">{{ t("sharedlg.policy") }}</span>
        <div class="grid grid-cols-3 gap-2">
          <label class="flex flex-col gap-1">
            <span class="text-xs text-[var(--text-3)]">{{ t("sharedlg.expires") }}</span>
            <Input v-model="expiresAt" type="date" />
          </label>
          <label class="flex flex-col gap-1">
            <span class="text-xs text-[var(--text-3)]">{{ t("sharedlg.password") }}</span>
            <Input
              v-model="password"
              type="password"
              autocomplete="off"
              :placeholder="t('sharedlg.password_ph')"
            />
          </label>
          <label class="flex flex-col gap-1">
            <span class="text-xs text-[var(--text-3)]">{{ t("sharedlg.max_visits") }}</span>
            <Input
              v-model="maxVisits"
              type="number"
              min="1"
              :placeholder="t('sharedlg.unlimited')"
            />
          </label>
        </div>
        <span class="text-xs text-[var(--text-3)]">{{ t("sharedlg.policy_hint") }}</span>
      </div>

      <!-- 多库联查（可选）：勾选副库，匿名问答同时检索（对标登录端 + 联查） -->
      <div v-if="otherKbs.length" class="flex flex-col gap-1.5">
        <span class="text-sm text-[var(--text-2)]">{{ t("sharedlg.joint_pick") }}</span>
        <div class="flex flex-wrap gap-x-4 gap-y-1.5">
          <label
            v-for="kb in otherKbs"
            :key="kb.id"
            class="flex cursor-pointer items-center gap-1.5 text-sm text-[var(--text-2)]"
          >
            <input
              type="checkbox"
              class="accent-[var(--accent)]"
              :checked="extraKbIds.includes(kb.id)"
              @change="toggleExtra(kb.id)"
            />
            <span class="truncate">{{ kb.name }}</span>
          </label>
        </div>
      </div>

      <!-- 链接列表 -->
      <p v-if="!links.length" class="py-2 text-sm text-[var(--text-3)]">
        {{ t("sharedlg.empty") }}
      </p>
      <div v-else class="flex max-h-72 flex-col gap-2 overflow-y-auto">
        <div
          v-for="link in links"
          :key="link.id"
          class="rounded-[var(--radius-card)] border border-[var(--border)] bg-[var(--surface)] p-3"
        >
          <div class="mb-1.5 flex items-center justify-between gap-2">
            <span class="flex min-w-0 flex-wrap items-center gap-x-1 text-sm font-medium">
              {{ link.name || t("sharedlg.unnamed") }}
              <span class="text-xs font-normal text-[var(--text-3)]">
                {{ t("sharedlg.model_prefix") }}{{ link.provider || t("sharedlg.default_model") }}
              </span>
              <!-- 联查徽标：悬停可见完整库名清单 -->
              <span
                v-if="(link.kb_names?.length ?? 0) > 1"
                class="rounded bg-[var(--accent-weak)] px-1.5 py-0.5 text-xs font-normal text-[var(--accent-text)]"
                :title="link.kb_names.join(' · ')"
              >
                {{ t("sharedlg.joint_badge", { n: link.kb_names.length }) }}
              </span>
              <!-- T10 策略/用量徽标：只显示"有限制"的维度，无限制不占版面 -->
              <span
                v-if="expiryText(link)"
                class="rounded bg-[var(--surface-2)] px-1.5 py-0.5 text-xs font-normal text-[var(--text-3)]"
              >
                {{ t("sharedlg.badge_expires", { date: expiryText(link) }) }}
              </span>
              <span
                v-if="link.has_password"
                class="rounded bg-[var(--warn-weak)] px-1.5 py-0.5 text-xs font-normal text-[var(--warn)]"
              >
                {{ t("sharedlg.badge_password") }}
              </span>
              <span
                v-if="visitText(link)"
                class="rounded bg-[var(--surface-2)] px-1.5 py-0.5 text-xs font-normal text-[var(--text-3)]"
              >
                {{ visitText(link) }}
              </span>
            </span>
            <button
              type="button"
              class="rounded p-1 text-[var(--text-3)] transition-colors hover:bg-[var(--err-weak)] hover:text-[var(--err)]"
              :title="t('sharedlg.revoke')"
              @click="revoke(link)"
            >
              <Trash2 class="size-3.5" />
            </button>
          </div>
          <div class="flex items-center gap-1.5">
            <code class="min-w-0 flex-1 truncate rounded bg-[var(--surface-2)] px-2 py-1 text-xs">
              {{ shareUrl(link) }}
            </code>
            <Button size="sm" variant="outline" @click="copy(shareUrl(link), t('sharedlg.link'))">
              <Copy class="size-3" />
              {{ t("sharedlg.link") }}
            </Button>
            <Button size="sm" variant="outline" @click="copy(embedSnippet(link), t('sharedlg.embed'))">
              <Copy class="size-3" />
              {{ t("sharedlg.embed") }}
            </Button>
          </div>
        </div>
      </div>
    </DialogContent>
  </Dialog>
</template>
