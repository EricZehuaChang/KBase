<script setup lang="ts">
// 用户管理卡片（设置页，仅 admin 可见）：列表 + 角色变更 Select + 禁用/启用
// Switch；新建用户/重置密码两个 Dialog 拆到 UserFormDialogs.vue（>200 行拆分
// 约定）。"不能禁用/降级最后一个管理员"的真正强制在后端（422 中文 detail，
// 见 kbase/api/main.py update_user）；isLastEnabledAdmin 只做前置按钮禁用，
// 减少用户点了才被拒绝的挫败感，不是安全边界。
import { onMounted, ref } from "vue";
import { toast } from "vue-sonner";
import { Plus, KeyRound } from "@lucide/vue";
import { Button } from "@/components/ui/button";
import { Switch } from "@/components/ui/switch";
import {
  Select, SelectContent, SelectGroup, SelectItem, SelectTrigger, SelectValue,
} from "@/components/ui/select";
import {
  Table, TableHeader, TableBody, TableRow, TableHead, TableCell, TableEmpty,
} from "@/components/ui/table";
import UserFormDialogs from "@/components/UserFormDialogs.vue";
import { listUsers, updateUser, type UserItem } from "@/lib/api";
import { isLastEnabledAdmin } from "@/lib/settings-utils";
import { roleLabel } from "@/lib/auth-utils";

const ROLES = ["admin", "editor", "viewer"] as const;

const users = ref<UserItem[]>([]);
const loading = ref(true);

async function load() {
  loading.value = true;
  try {
    users.value = await listUsers();
  } catch (err) {
    toast.error(err instanceof Error ? err.message : String(err));
  } finally {
    loading.value = false;
  }
}

onMounted(load);

const createOpen = ref(false);
const resetTarget = ref<UserItem | null>(null);

async function changeRole(user: UserItem, role: string) {
  if (role === user.role) return;
  try {
    await updateUser(user.id, { role });
    toast.success(`已将 ${user.username} 的角色改为 ${roleLabel(role)}`);
  } catch (err) {
    toast.error(err instanceof Error ? err.message : String(err));
  } finally {
    await load();
  }
}

async function toggleDisabled(user: UserItem, disabled: boolean) {
  try {
    await updateUser(user.id, { disabled });
    toast.success(disabled ? `已禁用: ${user.username}` : `已启用: ${user.username}`);
  } catch (err) {
    toast.error(err instanceof Error ? err.message : String(err));
  } finally {
    await load();
  }
}
</script>

<template>
  <section class="rounded-[var(--radius-card)] border border-[var(--border)] bg-[var(--surface)] p-4">
    <div class="mb-3 flex items-center justify-between">
      <h2 class="text-sm font-medium text-[var(--text-2)]">用户管理</h2>
      <Button size="sm" @click="createOpen = true">
        <Plus class="size-3.5" />
        新建用户
      </Button>
    </div>

    <Table>
      <TableHeader>
        <TableRow>
          <TableHead>用户名</TableHead>
          <TableHead>邮箱</TableHead>
          <TableHead>角色</TableHead>
          <TableHead>状态</TableHead>
          <TableHead class="w-40">操作</TableHead>
        </TableRow>
      </TableHeader>
      <TableBody>
        <TableEmpty v-if="!loading && users.length === 0" :colspan="5">暂无用户</TableEmpty>
        <TableRow v-for="u in users" :key="u.id">
          <TableCell>{{ u.username }}</TableCell>
          <TableCell class="text-[var(--text-3)]">{{ u.email ?? "—" }}</TableCell>
          <TableCell>
            <Select
              :model-value="u.role"
              :disabled="isLastEnabledAdmin(users, u.id)"
              @update:model-value="(v) => changeRole(u, String(v))"
            >
              <SelectTrigger class="w-28"><SelectValue /></SelectTrigger>
              <SelectContent>
                <SelectGroup>
                  <SelectItem v-for="r in ROLES" :key="r" :value="r">{{ roleLabel(r) }}</SelectItem>
                </SelectGroup>
              </SelectContent>
            </Select>
          </TableCell>
          <TableCell>
            <label class="flex items-center gap-2">
              <Switch
                :model-value="!u.disabled"
                :disabled="isLastEnabledAdmin(users, u.id)"
                @update:model-value="(v) => toggleDisabled(u, !v)"
              />
              <span class="text-sm text-[var(--text-2)]">{{ u.disabled ? "已禁用" : "启用中" }}</span>
            </label>
          </TableCell>
          <TableCell>
            <Button variant="ghost" size="sm" @click="resetTarget = u">
              <KeyRound class="size-3.5" />
              重置密码
            </Button>
          </TableCell>
        </TableRow>
      </TableBody>
    </Table>
  </section>

  <UserFormDialogs
    v-model:create-open="createOpen"
    v-model:reset-target="resetTarget"
    @changed="load"
  />
</template>
