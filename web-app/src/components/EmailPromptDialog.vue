<script setup lang="ts">
// 首登邮箱引导：会话探测发现账号没绑邮箱时弹出（Portal/Admin Shell 挂载）。
// 邮箱是忘记密码重置的唯一通道，所以开始就让用户补上；仍留"稍后再说"
// （本次会话内不再弹，sessionStorage 记忆），不做死锁强制。
import { ref } from "vue";
import { useI18n } from "vue-i18n";
import { toast } from "vue-sonner";
import { Button } from "@/components/ui/button";
import { Input } from "@/components/ui/input";
import {
  Dialog, DialogContent, DialogHeader, DialogTitle, DialogDescription, DialogFooter,
} from "@/components/ui/dialog";
import { updateProfile } from "@/lib/api";

const open = defineModel<boolean>("open", { required: true });
const emit = defineEmits<{ saved: [email: string] }>();
const { t } = useI18n();

const email = ref("");
const busy = ref(false);
const error = ref<string | null>(null);

// 邮箱格式弱校验：够拦手滑（少@、带空格），不追 RFC 完备
const EMAIL_RE = /^\S+@\S+\.\S+$/;

async function save() {
  const value = email.value.trim();
  if (!EMAIL_RE.test(value)) {
    error.value = t("email.invalid");
    return;
  }
  busy.value = true;
  error.value = null;
  try {
    await updateProfile(value);
    toast.success(t("email.bound"));
    emit("saved", value);
    open.value = false;
  } catch (err) {
    error.value = err instanceof Error ? err.message : String(err);
  } finally {
    busy.value = false;
  }
}

function later() {
  // 本次浏览器会话内不再打扰；下次登录仍会提醒（直到绑定为止）
  sessionStorage.setItem("kbase_email_prompt_dismissed", "1");
  open.value = false;
}
</script>

<template>
  <Dialog v-model:open="open">
    <DialogContent class="sm:max-w-[400px]" @interact-outside.prevent @escape-key-down.prevent>
      <DialogHeader>
        <DialogTitle>{{ t("email.title") }}</DialogTitle>
        <DialogDescription>
          {{ t("email.desc") }}
        </DialogDescription>
      </DialogHeader>

      <label class="flex flex-col gap-1">
        <span class="text-sm text-[var(--text-2)]">{{ t("email.label") }}</span>
        <Input v-model="email" type="email" autofocus placeholder="you@company.com" @keydown.enter.prevent="save" />
      </label>

      <p v-if="error" class="rounded-[var(--radius-ctl)] bg-[var(--err-weak)] px-3 py-2 text-sm text-[var(--err)]">
        {{ error }}
      </p>

      <DialogFooter>
        <Button variant="outline" :disabled="busy" @click="later">{{ t("email.later") }}</Button>
        <Button :disabled="busy || !email.trim()" @click="save">
          {{ busy ? t("email.saving") : t("email.bind") }}
        </Button>
      </DialogFooter>
    </DialogContent>
  </Dialog>
</template>
