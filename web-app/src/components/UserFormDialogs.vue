<script setup lang="ts">
// 新建用户 / 重置密码两个 Dialog，从 UserManagementCard 拆出（>200 行拆分
// 约定）。用 v-model 双向绑定父组件的 createOpen/resetTarget，成功后 emit
// changed 让父组件重新拉取列表。
import { reactive, ref, watch } from "vue";
import { useI18n } from "vue-i18n";
import { toast } from "vue-sonner";
import { Button } from "@/components/ui/button";
import { Input } from "@/components/ui/input";
import {
  Select, SelectContent, SelectGroup, SelectItem, SelectTrigger, SelectValue,
} from "@/components/ui/select";
import {
  Dialog, DialogContent, DialogHeader, DialogTitle, DialogDescription, DialogFooter,
} from "@/components/ui/dialog";
import { createUser, updateUser, type UserItem } from "@/lib/api";

const { t } = useI18n();

const ROLES = ["admin", "editor", "viewer"] as const;

const props = defineProps<{ createOpen: boolean; resetTarget: UserItem | null }>();
const emit = defineEmits<{
  "update:createOpen": [value: boolean];
  "update:resetTarget": [value: UserItem | null];
  changed: [];
}>();

// ---- 新建用户 ----
const creating = ref(false);
const newUser = reactive({ username: "", role: "viewer" as string, password: "",
                           email: "" });

watch(() => props.createOpen, (isOpen) => {
  if (!isOpen) return;
  newUser.username = "";
  newUser.role = "viewer";
  newUser.password = "";
  newUser.email = "";
});

async function submitCreate() {
  if (!newUser.username.trim() || !newUser.password) return;
  creating.value = true;
  try {
    await createUser({
      username: newUser.username.trim(), role: newUser.role, password: newUser.password,
      email: newUser.email.trim() || undefined,
    });
    toast.success(t("user.created", { name: newUser.username }));
    emit("update:createOpen", false);
    emit("changed");
  } catch (err) {
    toast.error(err instanceof Error ? err.message : String(err));
  } finally {
    creating.value = false;
  }
}

// ---- 重置密码 ----
const newPassword = ref("");
const resetting = ref(false);

watch(() => props.resetTarget, () => {
  newPassword.value = "";
});

async function submitReset() {
  if (!props.resetTarget || !newPassword.value) return;
  resetting.value = true;
  try {
    await updateUser(props.resetTarget.id, { password: newPassword.value });
    toast.success(t("user.pw_reset", { name: props.resetTarget.username }));
    emit("update:resetTarget", null);
    emit("changed");
  } catch (err) {
    toast.error(err instanceof Error ? err.message : String(err));
  } finally {
    resetting.value = false;
  }
}
</script>

<template>
  <!-- 新建用户 Dialog -->
  <Dialog :open="createOpen" @update:open="(v) => emit('update:createOpen', v)">
    <DialogContent>
      <DialogHeader>
        <DialogTitle>{{ t("user.create") }}</DialogTitle>
        <DialogDescription>{{ t("user.create_desc") }}</DialogDescription>
      </DialogHeader>
      <div class="flex flex-col gap-3">
        <label class="flex flex-col gap-1">
          <span class="text-sm text-[var(--text-2)]">{{ t("login.username") }}</span>
          <Input v-model="newUser.username" :placeholder="t('login.username')" />
        </label>
        <label class="flex flex-col gap-1">
          <span class="text-sm text-[var(--text-2)]">{{ t("common.role_col") }}</span>
          <Select v-model="newUser.role">
            <SelectTrigger><SelectValue /></SelectTrigger>
            <SelectContent>
              <SelectGroup>
                <SelectItem v-for="r in ROLES" :key="r" :value="r">{{ t(`common.role.${r}`) }}</SelectItem>
              </SelectGroup>
            </SelectContent>
          </Select>
        </label>
        <label class="flex flex-col gap-1">
          <span class="text-sm text-[var(--text-2)]">{{ t("user.email_optional") }}</span>
          <Input v-model="newUser.email" type="email" placeholder="name@company.com" />
        </label>
        <label class="flex flex-col gap-1">
          <span class="text-sm text-[var(--text-2)]">{{ t("user.initial_pw") }}</span>
          <Input v-model="newUser.password" type="password" :placeholder="t('user.initial_pw_ph')" />
        </label>
      </div>
      <DialogFooter>
        <Button variant="outline" @click="emit('update:createOpen', false)">{{ t("common.cancel") }}</Button>
        <Button :disabled="creating || !newUser.username.trim() || !newUser.password" @click="submitCreate">
          {{ t("common.create") }}
        </Button>
      </DialogFooter>
    </DialogContent>
  </Dialog>

  <!-- 重置密码 Dialog -->
  <Dialog :open="!!resetTarget" @update:open="(v) => { if (!v) emit('update:resetTarget', null); }">
    <DialogContent>
      <DialogHeader>
        <DialogTitle>{{ t("user.reset_pw") }}</DialogTitle>
        <DialogDescription>{{ t("user.reset_desc", { name: resetTarget?.username }) }}</DialogDescription>
      </DialogHeader>
      <Input v-model="newPassword" type="password" :placeholder="t('user.new_pw_ph')" @keydown.enter="submitReset" />
      <DialogFooter>
        <Button variant="outline" @click="emit('update:resetTarget', null)">{{ t("common.cancel") }}</Button>
        <Button :disabled="resetting || !newPassword" @click="submitReset">{{ t("login.confirm_reset") }}</Button>
      </DialogFooter>
    </DialogContent>
  </Dialog>
</template>
