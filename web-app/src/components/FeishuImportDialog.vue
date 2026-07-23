<script setup lang="ts">
// 飞书知识库导入对话框（库详情页入口）：粘贴 wiki 节点链接（导该子树）
// 或 space_id（导整个空间）。打开时探测凭据状态——未配置就在对话框内
// 就地输入 app_id/app_secret（先存到设置再导入），不逼用户跳去设置页；
// 已配置则只显示当前 App ID，一步直达导入。
import { computed, ref, watch } from "vue";
import { useI18n, I18nT } from "vue-i18n";
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
const { t } = useI18n();

const status = ref<FeishuStatus | null>(null);
const source = ref("");
const appId = ref("");
const appSecret = ref("");
const importing = ref(false);
// 错误常驻对话框内（生产反馈：toast 一闪而过，用户只看到"没反应"）
const error = ref<string | null>(null);

// 已配置态下"一键开通权限"深链（带 status.app_id）；未配置无 app_id 不渲染。
const oneClickAuthUrl = computed(() =>
  status.value?.configured
    ? `https://open.feishu.cn/app/${status.value.app_id}/auth`
      + "?q=wiki:wiki:readonly,docx:document:readonly,drive:drive:readonly,"
      + "docs:doc:readonly,sheets:spreadsheet:readonly,bitable:app:readonly,"
      + "board:whiteboard:node:read&op_from=openapi&token_type=tenant"
    : null);

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
        error.value = t("feishuimport.need_creds");
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
      error.value = t("feishuimport.all_failed", { count: r.accepted.length, reason: first.slice(0, 160) });
      return;
    }
    const failed = r.accepted.length - r.total;
    toast.success(t("feishuimport.imported", { count: r.total })
      + (failed > 0 ? t("feishuimport.imported_failed", { failed }) : ""));
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
        <DialogTitle>{{ t("feishuimport.title") }}</DialogTitle>
        <DialogDescription>
          {{ t("feishuimport.desc") }}
        </DialogDescription>
      </DialogHeader>

      <div class="flex flex-col gap-3">
        <!-- 凭据区：未配置就地输入 + 完整三步指引；已配置收成一行。三步指引
        与已配置说明的动态 href/bold 用 <I18nT> 具名 slot，文案主体进 i18n key
        （bold 片段与「飞书开放平台」等复用 feishu.setup.* 共享键）。 -->
        <div
          v-if="status && !status.configured"
          class="flex flex-col gap-2 rounded-[var(--radius-ctl)] border border-[var(--warn)] bg-[var(--warn-weak)] p-3"
        >
          <p class="text-xs font-medium text-[var(--warn)]">{{ t("feishuimport.setup_title") }}</p>
          <ol class="list-decimal pl-4 text-xs leading-relaxed text-[var(--text-2)]">
            <li>
              <I18nT keypath="feishuimport.setup_step1" tag="span" scope="global">
                <template #platform>
                  <a href="https://open.feishu.cn/app" target="_blank" rel="noopener" class="text-[var(--accent-text)] underline">{{ t("feishu.setup.platform_name") }}</a>
                </template>
              </I18nT>
            </li>
            <li>
              <I18nT keypath="feishuimport.setup_step2" tag="span" scope="global">
                <template #allReadonly><b>{{ t("feishu.setup.perm_all_readonly") }}</b></template>
                <template #publish><b>{{ t("feishu.setup.perm_publish") }}</b></template>
              </I18nT>
            </li>
            <li>
              <I18nT keypath="feishuimport.setup_step3" tag="span" scope="global">
                <template #addMember><b>{{ t("feishu.setup.add_member") }}</b></template>
              </I18nT>
            </li>
          </ol>
          <Input v-model="appId" :placeholder="t('feishu.appid_ph')" />
          <Input v-model="appSecret" type="password" :placeholder="t('feishuimport.secret_ph')" />
        </div>
        <p v-else-if="status?.configured" class="text-xs text-[var(--text-3)]">
          <I18nT keypath="feishuimport.configured_note" tag="span" scope="global">
            <template #appId><b>{{ status.app_id }}</b></template>
            <template #oneClickPerm>
              <a
                v-if="oneClickAuthUrl"
                :href="oneClickAuthUrl"
                target="_blank" rel="noopener" class="text-[var(--accent-text)] underline"
              >{{ t("feishuimport.one_click_perm") }}</a>
            </template>
          </I18nT>
        </p>

        <Input
          v-model="source"
          :placeholder="t('feishuimport.source_ph')"
          :aria-label="t('feishuimport.source_label')"
          @keydown.enter="doImport"
        />

        <!-- 错误常驻展示（不随 toast 消失）+ 高频原因排查提示 -->
        <div
          v-if="error"
          class="rounded-[var(--radius-ctl)] border border-[var(--err)] bg-[var(--err-weak)] p-3 text-xs"
        >
          <p class="font-medium text-[var(--err)]">{{ t("feishuimport.import_failed") }}{{ error }}</p>
          <I18nT keypath="feishuimport.troubleshoot" tag="p" scope="global" class="mt-1.5 text-[var(--text-2)]">
            <template #publish><b>{{ t("feishuimport.publish_short") }}</b></template>
            <template #addKbMember><b>{{ t("feishuimport.add_kb_member") }}</b></template>
          </I18nT>
        </div>
      </div>

      <DialogFooter>
        <Button variant="outline" @click="emit('update:open', false)">{{ t("common.cancel") }}</Button>
        <Button :disabled="importing || !source.trim()" @click="doImport">
          {{ importing ? t("feishuimport.importing") : t("feishuimport.start") }}
        </Button>
      </DialogFooter>
    </DialogContent>
  </Dialog>
</template>
