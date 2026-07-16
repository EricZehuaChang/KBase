<script setup lang="ts">
// 自助修改密码对话框（使用端/管理端顶栏用户区共用）：旧密码复核 + 新密码
// 二次确认。成功后提示重新登录不强制踢出——会话 token 仍有效至过期，
// 强制全端下线属后续增强。
import { ref, watch } from "vue";
import { toast } from "vue-sonner";
import { Button } from "@/components/ui/button";
import {
  Dialog, DialogContent, DialogHeader, DialogTitle, DialogDescription, DialogFooter,
} from "@/components/ui/dialog";
import { Input } from "@/components/ui/input";
import { changePassword } from "@/lib/api";

const props = defineProps<{ open: boolean }>();
const emit = defineEmits<{ "update:open": [value: boolean] }>();

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
    error.value = "新密码至少 6 位";
    return;
  }
  if (newPassword.value !== confirmPassword.value) {
    error.value = "两次输入的新密码不一致";
    return;
  }
  busy.value = true;
  try {
    await changePassword(oldPassword.value, newPassword.value);
    toast.success("密码已修改，下次登录请使用新密码");
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
        <DialogTitle>修改密码</DialogTitle>
        <DialogDescription>需先输入当前密码验证身份</DialogDescription>
      </DialogHeader>
      <div class="flex flex-col gap-3">
        <Input v-model="oldPassword" type="password" placeholder="当前密码" autocomplete="current-password" />
        <Input v-model="newPassword" type="password" placeholder="新密码（至少 6 位）" autocomplete="new-password" />
        <Input
          v-model="confirmPassword"
          type="password"
          placeholder="再次输入新密码"
          autocomplete="new-password"
          @keydown.enter="submit"
        />
        <p v-if="error" class="rounded-[var(--radius-ctl)] bg-[var(--err-weak)] px-3 py-2 text-sm text-[var(--err)]">
          {{ error }}
        </p>
      </div>
      <DialogFooter>
        <Button variant="outline" @click="emit('update:open', false)">取消</Button>
        <Button :disabled="busy || !oldPassword || !newPassword" @click="submit">确认修改</Button>
      </DialogFooter>
    </DialogContent>
  </Dialog>
</template>
