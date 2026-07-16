<script setup lang="ts">
// 飞书知识库导入对话框（库详情页入口）：粘贴 wiki 节点链接（导该子树）
// 或 space_id（导整个空间）。打开时探测凭据状态——未配置就在对话框内
// 就地输入 app_id/app_secret（先存到设置再导入），不逼用户跳去设置页；
// 已配置则只显示当前 App ID，一步直达导入。
import { ref, watch } from "vue";
import { toast } from "vue-sonner";
import { Button } from "@/components/ui/button";
import {
  Dialog, DialogContent, DialogHeader, DialogTitle, DialogDescription, DialogFooter,
} from "@/components/ui/dialog";
import { Input } from "@/components/ui/input";
import {
  getFeishuStatus, putFeishuCredentials, importFeishu, type FeishuStatus,
} from "@/lib/api";

const props = defineProps<{ open: boolean; kbId: string | undefined }>();
const emit = defineEmits<{ "update:open": [value: boolean]; imported: [] }>();

const status = ref<FeishuStatus | null>(null);
const source = ref("");
const appId = ref("");
const appSecret = ref("");
const importing = ref(false);

watch(() => props.open, async (isOpen) => {
  if (!isOpen) return;
  source.value = "";
  appId.value = "";
  appSecret.value = "";
  try {
    status.value = await getFeishuStatus();
  } catch {
    status.value = null;
  }
});

async function doImport() {
  if (!props.kbId || !source.value.trim()) return;
  importing.value = true;
  try {
    // 对话框里填了凭据 → 先保存（等价设置页配置，一次配置处处可用）
    if (!status.value?.configured) {
      if (!appId.value.trim() || !appSecret.value.trim()) {
        toast.error("请先填写飞书应用凭据（App ID 与 App Secret）");
        return;
      }
      await putFeishuCredentials(appId.value.trim(), appSecret.value.trim());
    }
    const r = await importFeishu(props.kbId, source.value.trim());
    toast.success(`已导入 ${r.total} 篇飞书文档，解析中（层级结构已保留）`);
    emit("imported");
    emit("update:open", false);
  } catch (err) {
    toast.error(err instanceof Error ? err.message : String(err));
  } finally {
    importing.value = false;
  }
}
</script>

<template>
  <Dialog :open="open" @update:open="(v) => emit('update:open', v)">
    <DialogContent>
      <DialogHeader>
        <DialogTitle>从飞书知识库导入</DialogTitle>
        <DialogDescription>
          粘贴 wiki 页面链接（导入该页及其全部子页面）或知识空间
          space_id（导入整个空间）；文档层级会保留进引用溯源
        </DialogDescription>
      </DialogHeader>

      <div class="flex flex-col gap-3">
        <!-- 凭据区：未配置就地输入；已配置只读展示 -->
        <div
          v-if="status && !status.configured"
          class="flex flex-col gap-2 rounded-[var(--radius-ctl)] border border-[var(--warn)] bg-[var(--warn-weak)] p-3"
        >
          <p class="text-xs text-[var(--warn)]">
            首次使用需配置飞书自建应用凭据（开放平台创建，授权 wiki 与
            docx 只读）；保存后全站生效，也可稍后在 设置 → 连接器 里维护
          </p>
          <Input v-model="appId" placeholder="App ID（cli_ 开头）" />
          <Input v-model="appSecret" type="password" placeholder="App Secret" />
        </div>
        <p v-else-if="status?.configured" class="text-xs text-[var(--text-3)]">
          使用已配置的飞书应用：{{ status.app_id }}（可在 设置 → 连接器 更换）
        </p>

        <Input
          v-model="source"
          placeholder="https://xxx.feishu.cn/wiki/… 或 space_id"
          aria-label="飞书 wiki 链接或空间 ID"
          @keydown.enter="doImport"
        />
      </div>

      <DialogFooter>
        <Button variant="outline" @click="emit('update:open', false)">取消</Button>
        <Button :disabled="importing || !source.trim()" @click="doImport">
          {{ importing ? "导入中（文档多时需等待）…" : "开始导入" }}
        </Button>
      </DialogFooter>
    </DialogContent>
  </Dialog>
</template>
