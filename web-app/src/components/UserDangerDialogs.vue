<script setup lang="ts">
// 超管专属的两个危险操作 Dialog（用户管理行内，仅超管可见入口）：
// ①改账号名——身份级操作：改名后该用户旧会话即失效须用新名重新登录，
//   历史审计保留旧名（对话框内明示后果）；
// ②删除账号——不可恢复：连带清理其私有数据（会话/消息/反馈/授权行），
//   团队资产（知识库/文档）不随人删。强确认：需输入该用户名才可点删除。
import { computed, ref, watch } from "vue";
import { useI18n } from "vue-i18n";
import { toast } from "vue-sonner";
import { Button } from "@/components/ui/button";
import { Input } from "@/components/ui/input";
import {
  Dialog, DialogContent, DialogHeader, DialogTitle, DialogDescription, DialogFooter,
} from "@/components/ui/dialog";
import { updateUser, deleteUser, type UserItem } from "@/lib/api";

const props = defineProps<{
  renameTarget: UserItem | null;
  deleteTarget: UserItem | null;
}>();
const emit = defineEmits<{
  "update:renameTarget": [value: UserItem | null];
  "update:deleteTarget": [value: UserItem | null];
  changed: [];
}>();
const { t } = useI18n();

// ---- 改账号名 ----
const newName = ref("");
const renaming = ref(false);
watch(() => props.renameTarget, (u) => { newName.value = u?.username ?? ""; });

async function submitRename() {
  const target = props.renameTarget;
  const name = newName.value.trim();
  if (!target || !name || name === target.username) return;
  renaming.value = true;
  try {
    await updateUser(target.id, { username: name });
    toast.success(t("user.renamed", { old: target.username, name }));
    emit("update:renameTarget", null);
    emit("changed");
  } catch (err) {
    toast.error(err instanceof Error ? err.message : String(err));
  } finally {
    renaming.value = false;
  }
}

// ---- 删除账号（输入用户名强确认） ----
const confirmName = ref("");
const deleting = ref(false);
watch(() => props.deleteTarget, () => { confirmName.value = ""; });
const deleteArmed = computed(() =>
  props.deleteTarget !== null
  && confirmName.value.trim() === props.deleteTarget.username);

async function submitDelete() {
  const target = props.deleteTarget;
  if (!target || !deleteArmed.value) return;
  deleting.value = true;
  try {
    await deleteUser(target.id);
    toast.success(t("user.deleted", { name: target.username }));
    emit("update:deleteTarget", null);
    emit("changed");
  } catch (err) {
    toast.error(err instanceof Error ? err.message : String(err));
  } finally {
    deleting.value = false;
  }
}
</script>

<template>
  <!-- 改账号名 -->
  <Dialog
    :open="renameTarget !== null"
    @update:open="(v) => !v && emit('update:renameTarget', null)"
  >
    <DialogContent class="sm:max-w-[400px]">
      <DialogHeader>
        <DialogTitle>{{ t("user.rename_title", { name: renameTarget?.username ?? "" }) }}</DialogTitle>
        <DialogDescription>{{ t("user.rename_note") }}</DialogDescription>
      </DialogHeader>
      <Input
        v-model="newName"
        :placeholder="t('login.username')"
        @keydown.enter="submitRename"
      />
      <DialogFooter>
        <Button variant="outline" @click="emit('update:renameTarget', null)">
          {{ t("common.cancel") }}
        </Button>
        <Button
          :disabled="renaming || !newName.trim() || newName.trim() === renameTarget?.username"
          @click="submitRename"
        >
          {{ t("common.save") }}
        </Button>
      </DialogFooter>
    </DialogContent>
  </Dialog>

  <!-- 删除账号（输入用户名解锁） -->
  <Dialog
    :open="deleteTarget !== null"
    @update:open="(v) => !v && emit('update:deleteTarget', null)"
  >
    <DialogContent class="sm:max-w-[420px]">
      <DialogHeader>
        <DialogTitle class="text-[var(--err)]">
          {{ t("user.delete_title", { name: deleteTarget?.username ?? "" }) }}
        </DialogTitle>
        <DialogDescription>{{ t("user.delete_warn") }}</DialogDescription>
      </DialogHeader>
      <label class="flex flex-col gap-1">
        <span class="text-sm text-[var(--text-2)]">
          {{ t("user.delete_confirm_hint", { name: deleteTarget?.username ?? "" }) }}
        </span>
        <Input v-model="confirmName" :placeholder="deleteTarget?.username ?? ''" />
      </label>
      <DialogFooter>
        <Button variant="outline" @click="emit('update:deleteTarget', null)">
          {{ t("common.cancel") }}
        </Button>
        <Button
          class="bg-[var(--err)] text-white hover:bg-[var(--err)]/90"
          :disabled="deleting || !deleteArmed"
          @click="submitDelete"
        >
          {{ t("common.confirm_delete") }}
        </Button>
      </DialogFooter>
    </DialogContent>
  </Dialog>
</template>
