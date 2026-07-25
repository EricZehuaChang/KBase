<script setup lang="ts">
// 用户管理卡片（设置页，仅 admin 可见）：列表 + 角色变更 Select + 禁用/启用
// Switch；新建用户/重置密码两个 Dialog 拆到 UserFormDialogs.vue（>200 行拆分
// 约定）。"不能禁用/降级最后一个管理员"的真正强制在后端（422 中文 detail，
// 见 kbase/api/main.py update_user）；isLastEnabledAdmin 只做前置按钮禁用，
// 减少用户点了才被拒绝的挫败感，不是安全边界。
import { computed, onMounted, ref } from "vue";
import { useI18n } from "vue-i18n";
import { toast } from "vue-sonner";
import { Plus, KeyRound, Mail, Pencil, Trash2 } from "@lucide/vue";
import { Button } from "@/components/ui/button";
import { Switch } from "@/components/ui/switch";
import {
  Select, SelectContent, SelectGroup, SelectItem, SelectTrigger, SelectValue,
} from "@/components/ui/select";
import {
  Table, TableHeader, TableBody, TableRow, TableHead, TableCell, TableEmpty,
} from "@/components/ui/table";
import UserFormDialogs from "@/components/UserFormDialogs.vue";
import UserInviteDialog from "@/components/UserInviteDialog.vue";
import UserDangerDialogs from "@/components/UserDangerDialogs.vue";
import { listUsers, updateUser, currentRole, type UserItem } from "@/lib/api";
import { isSuperadmin } from "@/lib/auth-utils";
import { isLastEnabledAdmin } from "@/lib/settings-utils";

const { t } = useI18n();

// 超管层级：superadmin 选项只对超管本人出现；超管账号的行对普通 admin
// 整行锁死（改角色/禁用/重置密码都 403，见后端 admin.py）——前置禁用防呆。
const iAmSuper = computed(() => isSuperadmin(currentRole.value ?? ""));
const roleOptions = computed(() =>
  iAmSuper.value ? ["superadmin", "admin", "editor", "viewer"]
                 : ["admin", "editor", "viewer"]);
function rowLocked(u: UserItem): boolean {
  return u.role === "superadmin" && !iAmSuper.value;
}

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
// 「邮箱与邀请」对话框目标（维护邮箱/发凭据邮件），超管行对普通 admin 锁定
const inviteTarget = ref<UserItem | null>(null);
// 超管专属危险操作：改账号名 / 删除账号（入口仅超管可见）
const renameTarget = ref<UserItem | null>(null);
const deleteTarget = ref<UserItem | null>(null);

async function changeRole(user: UserItem, role: string) {
  if (role === user.role) return;
  try {
    await updateUser(user.id, { role });
    toast.success(t("user.role_changed", { name: user.username, role: t(`common.role.${role}`) }));
  } catch (err) {
    toast.error(err instanceof Error ? err.message : String(err));
  } finally {
    await load();
  }
}

async function toggleAdvancedUi(user: UserItem, advancedUi: boolean) {
  try {
    await updateUser(user.id, { advanced_ui: advancedUi });
    toast.success(advancedUi ? t("user.adv_on", { name: user.username })
                             : t("user.adv_off", { name: user.username }));
  } catch (err) {
    toast.error(err instanceof Error ? err.message : String(err));
  } finally {
    await load();
  }
}

async function toggleDisabled(user: UserItem, disabled: boolean) {
  try {
    await updateUser(user.id, { disabled });
    toast.success(disabled ? t("user.disabled_toast", { name: user.username })
                           : t("user.enabled_toast", { name: user.username }));
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
      <h2 class="text-sm font-medium text-[var(--text-2)]">{{ t("user.title") }}</h2>
      <Button size="sm" @click="createOpen = true">
        <Plus class="size-3.5" />
        {{ t("user.create") }}
      </Button>
    </div>

    <Table>
      <TableHeader>
        <TableRow>
          <TableHead>{{ t("login.username") }}</TableHead>
          <TableHead>{{ t("user.col_email") }}</TableHead>
          <TableHead>{{ t("common.role_col") }}</TableHead>
          <TableHead>{{ t("common.status") }}</TableHead>
          <TableHead>{{ t("user.col_advanced") }}</TableHead>
          <TableHead class="w-40">{{ t("common.actions") }}</TableHead>
        </TableRow>
      </TableHeader>
      <TableBody>
        <TableEmpty v-if="!loading && users.length === 0" :colspan="6">{{ t("user.empty") }}</TableEmpty>
        <TableRow v-for="u in users" :key="u.id">
          <TableCell>
            <span class="inline-flex items-center gap-1">
              {{ u.username }}
              <!-- 改账号名：仅超管可见（身份级操作，后端二次强制） -->
              <button
                v-if="iAmSuper"
                type="button"
                class="rounded p-0.5 text-[var(--text-3)] transition-colors hover:bg-[var(--surface-2)] hover:text-[var(--text-2)]"
                :title="t('user.rename_title', { name: u.username })"
                @click="renameTarget = u"
              >
                <Pencil class="size-3" />
              </button>
            </span>
          </TableCell>
          <TableCell class="text-[var(--text-3)]">{{ u.email ?? "—" }}</TableCell>
          <TableCell>
            <Select
              :model-value="u.role"
              :disabled="isLastEnabledAdmin(users, u.id) || rowLocked(u)"
              @update:model-value="(v) => changeRole(u, String(v))"
            >
              <SelectTrigger class="w-28"><SelectValue /></SelectTrigger>
              <SelectContent>
                <SelectGroup>
                  <SelectItem v-for="r in roleOptions" :key="r" :value="r">{{ t(`common.role.${r}`) }}</SelectItem>
                </SelectGroup>
              </SelectContent>
            </Select>
          </TableCell>
          <TableCell>
            <label class="flex items-center gap-2">
              <Switch
                :model-value="!u.disabled"
                :disabled="isLastEnabledAdmin(users, u.id) || rowLocked(u)"
                @update:model-value="(v) => toggleDisabled(u, !v)"
              />
              <span class="text-sm text-[var(--text-2)]">{{ u.disabled ? t("user.disabled_state") : t("user.enabled_state") }}</span>
            </label>
          </TableCell>
          <TableCell>
            <!-- 模型选择/多库联查菜单可见性：viewer 按人开关；editor/admin
            恒可见（开关置灰常开），保持"一个判断源"心智 -->
            <Switch
              :model-value="u.role !== 'viewer' || u.advanced_ui"
              :disabled="u.role !== 'viewer'"
              :title="u.role !== 'viewer' ? t('user.adv_always') : t('user.adv_hint')"
              @update:model-value="(v) => toggleAdvancedUi(u, Boolean(v))"
            />
          </TableCell>
          <TableCell>
            <div class="flex items-center gap-1">
              <Button
                variant="ghost" size="sm"
                :disabled="rowLocked(u)"
                :title="rowLocked(u) ? t('user.superadmin_locked') : undefined"
                @click="resetTarget = u"
              >
                <KeyRound class="size-3.5" />
                {{ t("user.reset_pw") }}
              </Button>
              <!-- 邮箱与邀请：维护邮箱 + 发送凭据邮件（地址/账号/初始密码） -->
              <Button
                variant="ghost" size="sm"
                :disabled="rowLocked(u)"
                :title="rowLocked(u) ? t('user.superadmin_locked') : undefined"
                @click="inviteTarget = u"
              >
                <Mail class="size-3.5" />
                {{ t("user.invite_btn") }}
              </Button>
              <!-- 删除账号：仅超管可见；最后一个启用超管禁删（防锁死） -->
              <Button
                v-if="iAmSuper"
                variant="ghost" size="sm"
                class="text-[var(--err)] hover:bg-[var(--err-weak)] hover:text-[var(--err)]"
                :disabled="isLastEnabledAdmin(users, u.id)"
                :title="isLastEnabledAdmin(users, u.id) ? t('error.last_superadmin') : t('user.delete_title', { name: u.username })"
                @click="deleteTarget = u"
              >
                <Trash2 class="size-3.5" />
              </Button>
            </div>
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
  <UserInviteDialog v-model:target="inviteTarget" @changed="load" />
  <UserDangerDialogs
    v-model:rename-target="renameTarget"
    v-model:delete-target="deleteTarget"
    @changed="load"
  />
</template>
