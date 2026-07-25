<script setup lang="ts">
// 「邮箱与邀请」对话框（用户管理行内 Mail 按钮）：一处搞定两件事——
// ①维护邮箱（仅保存，不动密码不发信）②发送邀请邮件（设置新初始密码
// [可指定/留空随机]，邮件含 登录地址+账号+初始密码，按账号语言偏好选
// 中/英文模板）。发信由后端同步执行，失败会返回具体原因（如发件箱未配置）。
import { reactive, ref, watch } from "vue";
import { useI18n } from "vue-i18n";
import { toast } from "vue-sonner";
import { Button } from "@/components/ui/button";
import { Input } from "@/components/ui/input";
import {
  Dialog, DialogContent, DialogHeader, DialogTitle, DialogDescription,
} from "@/components/ui/dialog";
import { inviteUser, updateUser, type UserItem } from "@/lib/api";

const props = defineProps<{ target: UserItem | null }>();
const emit = defineEmits<{
  "update:target": [value: UserItem | null];
  changed: [];
}>();
const { t } = useI18n();

const form = reactive({ email: "", password: "" });
const savingEmail = ref(false);
const sending = ref(false);

watch(() => props.target, (u) => {
  form.email = u?.email ?? "";
  form.password = "";
});

function close() {
  emit("update:target", null);
}

async function saveEmail() {
  if (!props.target || !form.email.trim()) return;
  savingEmail.value = true;
  try {
    await updateUser(props.target.id, { email: form.email.trim() });
    toast.success(t("user.email_saved"));
    emit("changed");
  } catch (err) {
    toast.error(err instanceof Error ? err.message : String(err));
  } finally {
    savingEmail.value = false;
  }
}

async function sendInvite() {
  if (!props.target || !form.email.trim()) return;
  sending.value = true;
  try {
    const r = await inviteUser(props.target.id, {
      email: form.email.trim(),
      password: form.password.trim() || undefined,
    });
    toast.success(t("user.invite_sent", { email: r.email }));
    emit("changed");
    close();
  } catch (err) {
    toast.error(err instanceof Error ? err.message : String(err));
  } finally {
    sending.value = false;
  }
}
</script>

<template>
  <Dialog :open="target !== null" @update:open="(v) => !v && close()">
    <DialogContent class="sm:max-w-[440px]">
      <DialogHeader>
        <DialogTitle>{{ t("user.invite_title", { name: target?.username ?? "" }) }}</DialogTitle>
        <DialogDescription>{{ t("user.invite_desc") }}</DialogDescription>
      </DialogHeader>

      <div class="flex flex-col gap-3">
        <!-- 邮箱维护：可单独保存（不发信不动密码） -->
        <label class="flex flex-col gap-1">
          <span class="text-sm text-[var(--text-2)]">{{ t("user.invite_email_label") }}</span>
          <div class="flex gap-2">
            <Input
              v-model="form.email"
              type="email"
              class="flex-1"
              placeholder="name@company.com"
              @keydown.enter="saveEmail"
            />
            <Button
              variant="outline"
              :disabled="savingEmail || !form.email.trim()"
              @click="saveEmail"
            >
              {{ t("user.save_email") }}
            </Button>
          </div>
        </label>

        <div class="border-t border-[var(--border)]" />

        <!-- 邀请：设置新初始密码 + 发送凭据邮件 -->
        <label class="flex flex-col gap-1">
          <span class="text-sm text-[var(--text-2)]">{{ t("user.invite_pw_label") }}</span>
          <Input
            v-model="form.password"
            type="text"
            :placeholder="t('user.invite_pw_ph')"
          />
        </label>
        <p class="text-xs text-[var(--text-3)]">{{ t("user.invite_note") }}</p>
        <Button
          :disabled="sending || !form.email.trim()"
          @click="sendInvite"
        >
          {{ sending ? t("user.invite_sending") : t("user.send_invite") }}
        </Button>
      </div>
    </DialogContent>
  </Dialog>
</template>
