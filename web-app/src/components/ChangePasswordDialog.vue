<script setup lang="ts">
// 自助修改密码对话框（使用端/管理端顶栏用户区共用）：旧密码复核 + 新密码
// 二次确认。成功后提示重新登录不强制踢出——会话 token 仍有效至过期，
// 强制全端下线属后续增强。
import { ref, watch } from "vue";
import { useI18n } from "vue-i18n";
import { toast } from "vue-sonner";
import { Button } from "@/components/ui/button";
import {
  Dialog, DialogContent, DialogHeader, DialogTitle, DialogDescription, DialogFooter,
} from "@/components/ui/dialog";
import { Input } from "@/components/ui/input";
import { changePassword } from "@/lib/api";

const props = defineProps<{ open: boolean }>();
const emit = defineEmits<{ "update:open": [value: boolean] }>();
const { t } = useI18n();

const oldPassword = ref("");
const newPassword = ref("");
const confirmPassword = ref("");
const error = ref<string | null>(null);
const busy = ref(false);

watch(() => props.open, (isOpen) => {
  if (!isOpen) return;
  oldPassword.value = "";
  newPassword.value = "";
  confirmPassword.value = "";
  error.value = null;
});

async function submit() {
  error.value = null;
  if (newPassword.value.length < 6) {
    error.value = t("login.pwd_min");
    return;
  }
  if (newPassword.value !== confirmPassword.value) {
    error.value = t("login.pwd_mismatch");
    return;
  }
  busy.value = true;
  try {
    await changePassword(oldPassword.value, newPassword.value);
    toast.success(t("pwd.changed"));
    emit("update:open", false);
  } catch (err) {
    error.value = err instanceof Error ? err.message : String(err);
  } finally {
    busy.value = false;
  }
}
</script>

<template>
  <Dialog :open="open" @update:open="(v) => emit('update:open', v)">
    <DialogContent>
      <DialogHeader>
        <DialogTitle>{{ t("portal.topbar.change_pw") }}</DialogTitle>
        <DialogDescription>{{ t("pwd.verify_hint") }}</DialogDescription>
      </DialogHeader>
      <div class="flex flex-col gap-3">
        <Input v-model="oldPassword" type="password" :placeholder="t('pwd.current')" autocomplete="current-password" />
        <Input v-model="newPassword" type="password" :placeholder="t('login.new_pwd')" autocomplete="new-password" />
        <Input
          v-model="confirmPassword"
          type="password"
          :placeholder="t('pwd.confirm_again')"
          autocomplete="new-password"
          @keydown.enter="submit"
        />
        <p v-if="error" class="rounded-[var(--radius-ctl)] bg-[var(--err-weak)] px-3 py-2 text-sm text-[var(--err)]">
          {{ error }}
        </p>
      </div>
      <DialogFooter>
        <Button variant="outline" @click="emit('update:open', false)">{{ t("common.cancel") }}</Button>
        <Button :disabled="busy || !oldPassword || !newPassword" @click="submit">{{ t("pwd.confirm") }}</Button>
      </DialogFooter>
    </DialogContent>
  </Dialog>
</template>
