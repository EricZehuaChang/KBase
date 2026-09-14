<script setup lang="ts">
// 编辑 API Key（T09）：启停开关 + 策略字段（有效期/配额/IP 白名单）。
// 与"新建"分开：新建是发一把新钥匙，编辑是改现有钥匙的限制——role 与库
// 白名单不在这里改（换权限=换钥匙，后端 PATCH 也不收这两项）。
import { reactive, ref, watch } from "vue";
import { useI18n } from "vue-i18n";
import { toast } from "vue-sonner";
import { Button } from "@/components/ui/button";
import { Switch } from "@/components/ui/switch";
import {
  Dialog, DialogContent, DialogHeader, DialogTitle, DialogDescription, DialogFooter,
} from "@/components/ui/dialog";
import ApiKeyPolicyFields from "@/components/ApiKeyPolicyFields.vue";
import { apiKeyPolicyFormFrom, buildApiKeyPolicy } from "@/lib/settings-utils";
import { updateApiKey, type ApiKeyItem } from "@/lib/api";

const props = defineProps<{ target: ApiKeyItem | null }>();
const emit = defineEmits<{
  "update:target": [value: ApiKeyItem | null];
  changed: [];
}>();

const { t } = useI18n();
const saving = ref(false);
const form = reactive({
  disabled: false,
  expiresAt: "",
  rpm: "",
  dailyQuota: "",
  ipAllow: "",
});

// 打开时用当前行回填（清空输入框=清除该项限制，语义见 settings-utils）
watch(() => props.target, (k) => {
  if (!k) return;
  form.disabled = k.disabled;
  Object.assign(form, apiKeyPolicyFormFrom(k));
});

async function save() {
  if (!props.target) return;
  saving.value = true;
  try {
    await updateApiKey(props.target.id, {
      disabled: form.disabled,
      ...buildApiKeyPolicy(form),
    });
    toast.success(t("apikey.saved_toast", { name: props.target.name }));
    emit("update:target", null);
    emit("changed");
  } catch (err) {
    toast.error(err instanceof Error ? err.message : String(err));
  } finally {
    saving.value = false;
  }
}
</script>

<template>
  <Dialog :open="!!target" @update:open="(v) => { if (!v) emit('update:target', null); }">
    <DialogContent class="max-h-[85vh] overflow-y-auto">
      <DialogHeader>
        <DialogTitle>{{ t("apikey.edit_title", { name: target?.name }) }}</DialogTitle>
        <DialogDescription>{{ t("apikey.edit_desc") }}</DialogDescription>
      </DialogHeader>
      <div class="flex flex-col gap-3">
        <div class="flex items-center gap-2">
          <Switch v-model="form.disabled" :aria-label="t('apikey.disabled_label')" />
          <span class="text-sm text-[var(--text-2)]">{{ t("apikey.disabled_label") }}</span>
          <span class="text-xs text-[var(--text-3)]">{{ t("apikey.disabled_hint") }}</span>
        </div>
        <ApiKeyPolicyFields
          v-model:expires-at="form.expiresAt"
          v-model:rpm="form.rpm"
          v-model:daily-quota="form.dailyQuota"
          v-model:ip-allow="form.ipAllow"
        />
      </div>
      <DialogFooter>
        <Button variant="outline" @click="emit('update:target', null)">{{ t("common.cancel") }}</Button>
        <Button :disabled="saving" @click="save">{{ t("common.save") }}</Button>
      </DialogFooter>
    </DialogContent>
  </Dialog>
</template>
