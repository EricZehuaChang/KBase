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
// 错误常驻对话框内（生产反馈：toast 一闪而过，用户只看到"没反应"）
const error = ref<string | null>(null);

watch(() => props.open, async (isOpen) => {
  if (!isOpen) return;
  source.value = "";
  appId.value = "";
  appSecret.value = "";
  error.value = null;
  try {
    status.value = await getFeishuStatus();
  } catch {
    status.value = null;
  }
});

async function doImport() {
  if (!props.kbId || !source.value.trim()) return;
  importing.value = true;
  error.value = null;
  try {
    // 对话框里填了凭据 → 先保存（等价设置页配置，一次配置处处可用）
    if (!status.value?.configured) {
      if (!appId.value.trim() || !appSecret.value.trim()) {
        error.value = "请先填写飞书应用凭据（App ID 与 App Secret）";
        return;
      }
      await putFeishuCredentials(appId.value.trim(), appSecret.value.trim());
      status.value = await getFeishuStatus();
    }
    const r = await importFeishu(props.kbId, source.value.trim());
    // total=0 = 树遍历成功但每篇正文都拉取失败（典型：docx 只读权限未
    // 开通/未发布）——按失败呈现并带出首条原因，绝不能报成功（生产
    // 实测用户被'已导入 0 篇'误导以为没反应）
    if (r.total === 0) {
      const first = r.accepted[0] ?? "";
      error.value = `共发现 ${r.accepted.length} 篇文档，但正文全部拉取失败。`
        + `首条原因：${first.slice(0, 160)}。若为 400/403，通常是应用缺少 `
        + `docx:document:readonly 权限（开通后记得发布版本）`;
      return;
    }
    const failed = r.accepted.length - r.total;
    toast.success(`已导入 ${r.total} 篇飞书文档，解析中（层级结构已保留）`
      + (failed > 0 ? `；${failed} 篇拉取失败已跳过` : ""));
    emit("imported");
    emit("update:open", false);
  } catch (err) {
    error.value = err instanceof Error ? err.message : String(err);
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

        <!-- 错误常驻展示（不随 toast 消失）+ 高频原因排查提示 -->
        <div
          v-if="error"
          class="rounded-[var(--radius-ctl)] border border-[var(--err)] bg-[var(--err-weak)] p-3 text-xs"
        >
          <p class="font-medium text-[var(--err)]">导入失败：{{ error }}</p>
          <p class="mt-1.5 text-[var(--text-2)]">
            常见原因排查：① 应用需在飞书开放平台开通并<b>发布版本</b>后权限才生效
            （wiki 与 docx 只读）；② 应用必须被<b>添加为该知识库的成员</b>
            （知识库设置 → 成员 → 添加应用），仅有权限而未入库同样读不到；
            ③ 链接需是 wiki 页面地址（含 /wiki/），云文档 /docx/ 链接暂不支持直导
          </p>
        </div>
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
