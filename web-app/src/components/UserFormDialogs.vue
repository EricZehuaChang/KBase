<script setup lang="ts">
// 新建用户 / 重置密码两个 Dialog，从 UserManagementCard 拆出（>200 行拆分
// 约定）。用 v-model 双向绑定父组件的 createOpen/resetTarget，成功后 emit
// changed 让父组件重新拉取列表。
import { reactive, ref, watch } from "vue";
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
    toast.success(`已创建用户: ${newUser.username}`);
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
    toast.success(`已重置 ${props.resetTarget.username} 的密码`);
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
        <DialogTitle>新建用户</DialogTitle>
        <DialogDescription>设置用户名、角色与初始密码</DialogDescription>
      </DialogHeader>
      <div class="flex flex-col gap-3">
        <label class="flex flex-col gap-1">
          <span class="text-sm text-[var(--text-2)]">用户名</span>
          <Input v-model="newUser.username" placeholder="用户名" />
        </label>
        <label class="flex flex-col gap-1">
          <span class="text-sm text-[var(--text-2)]">角色</span>
          <Select v-model="newUser.role">
            <SelectTrigger><SelectValue /></SelectTrigger>
            <SelectContent>
              <SelectGroup>
                <SelectItem v-for="r in ROLES" :key="r" :value="r">{{ r }}</SelectItem>
              </SelectGroup>
            </SelectContent>
          </Select>
        </label>
        <label class="flex flex-col gap-1">
          <span class="text-sm text-[var(--text-2)]">邮箱（选填，用于账号资料与后续找回密码）</span>
          <Input v-model="newUser.email" type="email" placeholder="name@company.com" />
        </label>
        <label class="flex flex-col gap-1">
          <span class="text-sm text-[var(--text-2)]">初始密码</span>
          <Input v-model="newUser.password" type="password" placeholder="初始密码（用户登录后可自行修改）" />
        </label>
      </div>
      <DialogFooter>
        <Button variant="outline" @click="emit('update:createOpen', false)">取消</Button>
        <Button :disabled="creating || !newUser.username.trim() || !newUser.password" @click="submitCreate">
          创建
        </Button>
      </DialogFooter>
    </DialogContent>
  </Dialog>

  <!-- 重置密码 Dialog -->
  <Dialog :open="!!resetTarget" @update:open="(v) => { if (!v) emit('update:resetTarget', null); }">
    <DialogContent>
      <DialogHeader>
        <DialogTitle>重置密码</DialogTitle>
        <DialogDescription>为「{{ resetTarget?.username }}」设置新密码</DialogDescription>
      </DialogHeader>
      <Input v-model="newPassword" type="password" placeholder="新密码" @keydown.enter="submitReset" />
      <DialogFooter>
        <Button variant="outline" @click="emit('update:resetTarget', null)">取消</Button>
        <Button :disabled="resetting || !newPassword" @click="submitReset">确认重置</Button>
      </DialogFooter>
    </DialogContent>
  </Dialog>
</template>
