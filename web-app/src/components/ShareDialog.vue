<script setup lang="ts">
// 分享链接管理对话框（KB 详情页，editor+）：建链接（备注+绑定回答模型，
// 对标 Dify/FastGPT——模型是建链接者的决策，终端用户无感）、复制链接/
// 嵌入代码、撤销。撤销立即生效（公开端点 404）。
import { onMounted, ref, watch } from "vue";
import { useI18n } from "vue-i18n";
import { toast } from "vue-sonner";
import { Copy, Trash2 } from "@lucide/vue";
import { Button } from "@/components/ui/button";
import { Input } from "@/components/ui/input";
import {
  Dialog, DialogContent, DialogHeader, DialogTitle, DialogDescription,
} from "@/components/ui/dialog";
import {
  Select, SelectContent, SelectGroup, SelectItem, SelectTrigger, SelectValue,
} from "@/components/ui/select";
import {
  createShareLink, listShareLinks, revokeShareLink, listProviders,
  type ShareLinkItem,
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
});
watch(open, (v) => { if (v) refresh(); }, { immediate: true });

async function create() {
  busy.value = true;
  try {
    await createShareLink(props.kbId, {
      name: name.value.trim(),
      provider: provider.value === "__default__" ? null : provider.value,
    });
    name.value = "";
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

async function copy(text: string, label: string) {
  await navigator.clipboard.writeText(text);
  toast.success(t("sharedlg.copied", { label }));
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
            <span class="truncate text-sm font-medium">
              {{ link.name || t("sharedlg.unnamed") }}
              <span class="ml-1 text-xs font-normal text-[var(--text-3)]">
                {{ t("sharedlg.model_prefix") }}{{ link.provider || t("sharedlg.default_model") }}
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
