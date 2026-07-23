<script setup lang="ts">
// 飞书连接器凭据卡片（设置页·连接器组，admin）：自建应用 app_id/app_secret
// 页面维护。app_id 是公开标识明文展示；secret 只写不回显（脱敏尾4位），
// 与 Provider/向量密钥同规矩。配好后库详情页"从飞书导入"即可用。
import { computed, onMounted, ref } from "vue";
import { useI18n, I18nT } from "vue-i18n";
import { toast } from "vue-sonner";
import { Button } from "@/components/ui/button";
import { Badge } from "@/components/ui/badge";
import { Input } from "@/components/ui/input";
import {
  getFeishuStatus, putFeishuCredentials, deleteFeishuCredentials,
  type FeishuStatus,
} from "@/lib/api";

const { t } = useI18n();

// 三步指引第 2 步的七项只读权限：权限码是飞书 API 的英文标识（不翻译，放
// <code> 具名 slot），说明进 i18n key（perm_* 值含 {code} 占位，由 slot 填回码）。
const SETUP_PERMS = [
  { code: "wiki:wiki:readonly", key: "feishu.setup.perm_wiki" },
  { code: "docx:document:readonly", key: "feishu.setup.perm_docx" },
  { code: "drive:drive:readonly", key: "feishu.setup.perm_drive" },
  { code: "docs:doc:readonly", key: "feishu.setup.perm_docs" },
  { code: "sheets:spreadsheet:readonly", key: "feishu.setup.perm_sheets" },
  { code: "bitable:app:readonly", key: "feishu.setup.perm_bitable" },
  { code: "board:whiteboard:node:read", key: "feishu.setup.perm_board" },
] as const;

const status = ref<FeishuStatus | null>(null);
const editing = ref(false);
const appId = ref("");
const appSecret = ref("");
const busy = ref(false);

// 一键开通全部只读权限的深链（带 status.app_id）；未配置凭据时无 app_id，
// 链接不渲染（perm_intro 的 oneClick slot 内 v-if 判断）。
const oneClickAuthUrl = computed(() =>
  status.value?.configured
    ? `https://open.feishu.cn/app/${status.value.app_id}/auth`
      + "?q=wiki:wiki:readonly,docx:document:readonly,drive:drive:readonly,"
      + "docs:doc:readonly,sheets:spreadsheet:readonly,bitable:app:readonly,"
      + "board:whiteboard:node:read&op_from=openapi&token_type=tenant"
    : null);

async function refresh() {
  try {
    status.value = await getFeishuStatus();
  } catch (err) {
    toast.error(err instanceof Error ? err.message : String(err));
  }
}

onMounted(refresh);

function startEdit() {
  editing.value = true;
  appId.value = status.value?.app_id ?? "";
  appSecret.value = "";
}

async function save() {
  if (!appId.value.trim() || !appSecret.value.trim()) return;
  busy.value = true;
  try {
    await putFeishuCredentials(appId.value.trim(), appSecret.value.trim());
    toast.success(t("feishu.creds_saved"));
    editing.value = false;
    await refresh();
  } catch (err) {
    toast.error(err instanceof Error ? err.message : String(err));
  } finally {
    busy.value = false;
  }
}

async function clearCredentials() {
  busy.value = true;
  try {
    await deleteFeishuCredentials();
    toast.success(t("feishu.creds_cleared"));
    await refresh();
  } catch (err) {
    toast.error(err instanceof Error ? err.message : String(err));
  } finally {
    busy.value = false;
  }
}
</script>

<template>
  <article class="rounded-[var(--radius-card)] border border-[var(--border)] bg-[var(--surface)] p-4">
    <div class="mb-1 flex items-center gap-2">
      <span class="font-medium">{{ t("feishu.title") }}</span>
      <Badge
        v-if="status"
        :class="status.configured
          ? 'bg-[var(--ok-weak)] text-[var(--ok)]'
          : 'bg-[var(--surface-2)] text-[var(--text-3)]'"
      >
        {{ status.configured ? t("smtp.configured") : t("smtp.not_configured") }}
      </Badge>
    </div>
    <p class="mb-2 text-xs text-[var(--text-3)]">
      {{ t("feishu.desc") }}
    </p>
    <!-- 三步配置指引常驻：权限体系是飞书侧最高频的卡点，必须事前讲清。含动态
    href（status.app_id）与交织的 code/link/bold——用 <I18nT> 具名 slot 把动态
    元素放槽、文案主体进 i18n key；权限码（wiki:wiki:readonly 等）是飞书 API 的
    英文标识，留在 <code> slot 不翻译。 -->
    <ol class="mb-3 list-decimal rounded-[var(--radius-ctl)] bg-[var(--surface-2)] py-2 pl-6 pr-3 text-xs leading-relaxed text-[var(--text-2)]">
      <li>
        <I18nT keypath="feishu.setup.step1" tag="span" scope="global">
          <template #platform>
            <a href="https://open.feishu.cn/app" target="_blank" rel="noopener" class="text-[var(--accent-text)] underline">{{ t("feishu.setup.platform_name") }}</a>
          </template>
        </I18nT>
      </li>
      <li>
        <I18nT keypath="feishu.setup.perm_intro" tag="span" scope="global">
          <template #allReadonly><b>{{ t("feishu.setup.perm_all_readonly") }}</b></template>
          <template #oneClick>
            <a
              v-if="oneClickAuthUrl"
              :href="oneClickAuthUrl"
              target="_blank" rel="noopener" class="text-[var(--accent-text)] underline"
            >{{ t("feishu.setup.one_click") }}</a>
          </template>
        </I18nT>
        <ul class="mt-1 list-disc pl-5">
          <li v-for="p in SETUP_PERMS" :key="p.code">
            <I18nT :keypath="p.key" tag="span" scope="global">
              <template #code><code class="rounded bg-black/5 px-1">{{ p.code }}</code></template>
            </I18nT>
          </li>
        </ul>
        <I18nT keypath="feishu.setup.perm_note" tag="span" scope="global">
          <template #review><b>{{ t("feishu.setup.perm_review") }}</b></template>
          <template #publish><b>{{ t("feishu.setup.perm_publish") }}</b></template>
        </I18nT>
      </li>
      <li>
        <I18nT keypath="feishu.setup.step3" tag="span" scope="global">
          <template #addMember><b>{{ t("feishu.setup.add_member") }}</b></template>
        </I18nT>
      </li>
    </ol>

    <div v-if="status?.configured && !editing" class="mb-3 flex items-center gap-3 text-sm">
      <span class="text-[var(--text-2)]">{{ t("feishu.app_id_label", { id: status.app_id }) }}</span>
      <span class="text-[var(--text-3)]">{{ t("feishu.secret_label", { hint: status.secret_hint }) }}</span>
    </div>

    <div v-if="editing" class="mb-3 flex flex-col gap-2">
      <Input v-model="appId" :placeholder="t('feishu.appid_ph')" />
      <Input v-model="appSecret" type="password" :placeholder="t('feishu.secret_ph')" />
      <div class="flex gap-2">
        <Button size="sm" :disabled="busy || !appId.trim() || !appSecret.trim()" @click="save">{{ t("common.save") }}</Button>
        <Button size="sm" variant="outline" @click="editing = false">{{ t("common.cancel") }}</Button>
      </div>
    </div>
    <div v-else class="flex gap-2">
      <Button size="sm" variant="outline" @click="startEdit">
        {{ status?.configured ? t("feishu.update_creds") : t("feishu.config_creds") }}
      </Button>
      <Button
        v-if="status?.configured"
        size="sm"
        variant="outline"
        :disabled="busy"
        @click="clearCredentials"
      >
        {{ t("feishu.clear") }}
      </Button>
    </div>
  </article>
</template>
