<script setup lang="ts">
// 分享链接管理对话框（KB 详情页，editor+）：建链接（备注+绑定回答模型，
// 对标 Dify/FastGPT——模型是建链接者的决策，终端用户无感）、复制链接/
// 嵌入代码、撤销。撤销立即生效（公开端点 404）。
import { onMounted, ref, watch } from "vue";
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
    toast.success("分享链接已创建");
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
  toast.success(`${label}已复制`);
}

async function revoke(link: ShareLinkItem) {
  try {
    await revokeShareLink(link.id);
    toast.success("已撤销，该链接立即失效");
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
        <DialogTitle>免登录分享</DialogTitle>
        <DialogDescription>
          持有链接者无需账号即可对本库问答（附引用溯源）；也可用嵌入代码把问答挂到任意网站。回答模型在此绑定，访客不可更改。
        </DialogDescription>
      </DialogHeader>

      <!-- 建链接 -->
      <div class="flex items-end gap-2">
        <label class="flex flex-1 flex-col gap-1">
          <span class="text-sm text-[var(--text-2)]">备注（给谁用）</span>
          <Input v-model="name" placeholder="如：官网客服窗口" @keydown.enter="create" />
        </label>
        <label class="flex w-40 flex-col gap-1">
          <span class="text-sm text-[var(--text-2)]">回答模型</span>
          <Select v-model="provider">
            <SelectTrigger><SelectValue /></SelectTrigger>
            <SelectContent>
              <SelectGroup>
                <SelectItem value="__default__">系统默认</SelectItem>
                <SelectItem v-for="p in providers" :key="p" :value="p">{{ p }}</SelectItem>
              </SelectGroup>
            </SelectContent>
          </Select>
        </label>
        <Button :disabled="busy" @click="create">创建</Button>
      </div>

      <!-- 链接列表 -->
      <p v-if="!links.length" class="py-2 text-sm text-[var(--text-3)]">
        暂无分享链接
      </p>
      <div v-else class="flex max-h-72 flex-col gap-2 overflow-y-auto">
        <div
          v-for="link in links"
          :key="link.id"
          class="rounded-[var(--radius-card)] border border-[var(--border)] bg-[var(--surface)] p-3"
        >
          <div class="mb-1.5 flex items-center justify-between gap-2">
            <span class="truncate text-sm font-medium">
              {{ link.name || "未命名链接" }}
              <span class="ml-1 text-xs font-normal text-[var(--text-3)]">
                模型：{{ link.provider || "系统默认" }}
              </span>
            </span>
            <button
              type="button"
              class="rounded p-1 text-[var(--text-3)] transition-colors hover:bg-[var(--err-weak)] hover:text-[var(--err)]"
              title="撤销（立即失效）"
              @click="revoke(link)"
            >
              <Trash2 class="size-3.5" />
            </button>
          </div>
          <div class="flex items-center gap-1.5">
            <code class="min-w-0 flex-1 truncate rounded bg-[var(--surface-2)] px-2 py-1 text-xs">
              {{ shareUrl(link) }}
            </code>
            <Button size="sm" variant="outline" @click="copy(shareUrl(link), '链接')">
              <Copy class="size-3" />
              链接
            </Button>
            <Button size="sm" variant="outline" @click="copy(embedSnippet(link), '嵌入代码')">
              <Copy class="size-3" />
              嵌入代码
            </Button>
          </div>
        </div>
      </div>
    </DialogContent>
  </Dialog>
</template>
