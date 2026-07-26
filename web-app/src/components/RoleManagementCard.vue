<script setup lang="ts">
// 角色管理卡片（设置页·用户与权限组，入口仅超管可见）：查看内置四角色的
// 权限（只读），创建/编辑/删除自定义角色——勾权限词表即定义该角色能做什么。
// 权限真实强制在后端（require_role 解析角色权限集合，见 kbase/auth/roles.py）；
// 本卡只负责维护定义。删除时后端拒绝"仍有用户在用"的角色（先改派再删）。
import { computed, onMounted, reactive, ref } from "vue";
import { useI18n } from "vue-i18n";
import { toast } from "vue-sonner";
import { Plus, Trash2, Pencil } from "@lucide/vue";
import { Button } from "@/components/ui/button";
import { Input } from "@/components/ui/input";
import {
  Dialog, DialogContent, DialogHeader, DialogTitle, DialogDescription, DialogFooter,
} from "@/components/ui/dialog";
import {
  listRoles, createRole, updateRole, deleteRole, type RoleItem,
} from "@/lib/api";

const emit = defineEmits<{ changed: [] }>();
const { t } = useI18n();

const roles = ref<RoleItem[]>([]);
const permissions = ref<string[]>([]);
const loading = ref(true);

async function load() {
  loading.value = true;
  try {
    const r = await listRoles();
    roles.value = r.roles;
    permissions.value = r.permissions;
  } catch (err) {
    toast.error(err instanceof Error ? err.message : String(err));
  } finally {
    loading.value = false;
  }
}
onMounted(load);

const customRoles = computed(() => roles.value.filter((r) => !r.builtin));
const builtinRoles = computed(() => roles.value.filter((r) => r.builtin));

// ---- 新建 / 编辑对话框 ----
const dialogOpen = ref(false);
const editingName = ref<string | null>(null);   // null=新建
const form = reactive({ name: "", label: "", permissions: [] as string[] });
const saving = ref(false);

function openCreate() {
  editingName.value = null;
  form.name = "";
  form.label = "";
  form.permissions = [];
  dialogOpen.value = true;
}

function openEdit(role: RoleItem) {
  editingName.value = role.name;
  form.name = role.name;
  form.label = role.label;
  form.permissions = [...role.permissions];
  dialogOpen.value = true;
}

function togglePerm(p: string) {
  form.permissions = form.permissions.includes(p)
    ? form.permissions.filter((x) => x !== p)
    : [...form.permissions, p];
}

async function submit() {
  if (!form.name.trim()) return;
  saving.value = true;
  try {
    if (editingName.value) {
      await updateRole(editingName.value, {
        label: form.label.trim(), permissions: form.permissions });
      toast.success(t("role.updated", { name: editingName.value }));
    } else {
      await createRole({ name: form.name.trim(), label: form.label.trim(),
                         permissions: form.permissions });
      toast.success(t("role.created", { name: form.name.trim() }));
    }
    dialogOpen.value = false;
    await load();
    emit("changed");      // 用户管理的角色下拉需要刷新
  } catch (err) {
    toast.error(err instanceof Error ? err.message : String(err));
  } finally {
    saving.value = false;
  }
}

async function remove(role: RoleItem) {
  try {
    await deleteRole(role.name);
    toast.success(t("role.deleted", { name: role.name }));
    await load();
    emit("changed");
  } catch (err) {
    toast.error(err instanceof Error ? err.message : String(err));
  }
}
</script>

<template>
  <article class="rounded-[var(--radius-card)] border border-[var(--border)] bg-[var(--surface)] p-4">
    <div class="mb-1 flex items-center justify-between gap-2">
      <span class="font-medium">{{ t("role.title") }}</span>
      <Button size="sm" @click="openCreate">
        <Plus class="size-3.5" />
        {{ t("role.new") }}
      </Button>
    </div>
    <p class="mb-3 text-xs text-[var(--text-3)]">{{ t("role.desc") }}</p>

    <p v-if="loading" class="text-sm text-[var(--text-3)]">{{ t("common.loading") }}</p>
    <template v-else>
      <!-- 内置角色（只读） -->
      <div class="mb-3 flex flex-wrap gap-2">
        <span
          v-for="r in builtinRoles"
          :key="r.name"
          class="rounded-[var(--radius-ctl)] bg-[var(--surface-2)] px-2 py-1 text-xs text-[var(--text-2)]"
          :title="r.permissions.map((p) => t(`role.perm.${p}`)).join(' · ') || t('role.no_perm')"
        >
          {{ t(`common.role.${r.name}`) }}
          <span class="text-[var(--text-3)]">· {{ t("role.builtin") }}</span>
        </span>
      </div>

      <!-- 自定义角色 -->
      <p v-if="!customRoles.length" class="text-sm text-[var(--text-3)]">
        {{ t("role.empty") }}
      </p>
      <div v-else class="flex flex-col gap-2">
        <div
          v-for="r in customRoles"
          :key="r.name"
          class="flex items-center gap-2 rounded-[var(--radius-ctl)] border border-[var(--border)] px-3 py-2"
        >
          <div class="min-w-0 flex-1">
            <div class="truncate text-sm font-medium">
              {{ r.label || r.name }}
              <code class="ml-1 rounded bg-[var(--surface-2)] px-1 text-xs font-normal">{{ r.name }}</code>
            </div>
            <div class="mt-0.5 text-xs text-[var(--text-3)]">
              <template v-if="r.permissions.length">
                {{ r.permissions.map((p) => t(`role.perm.${p}`)).join(" · ") }}
              </template>
              <template v-else>{{ t("role.no_perm") }}</template>
            </div>
          </div>
          <Button variant="ghost" size="sm" @click="openEdit(r)">
            <Pencil class="size-3.5" />
          </Button>
          <Button
            variant="ghost" size="sm"
            class="text-[var(--err)] hover:bg-[var(--err-weak)] hover:text-[var(--err)]"
            @click="remove(r)"
          >
            <Trash2 class="size-3.5" />
          </Button>
        </div>
      </div>
    </template>

    <!-- 新建/编辑对话框 -->
    <Dialog v-model:open="dialogOpen">
      <DialogContent class="sm:max-w-[440px]">
        <DialogHeader>
          <DialogTitle>
            {{ editingName ? t("role.edit_title", { name: editingName }) : t("role.new") }}
          </DialogTitle>
          <DialogDescription>{{ t("role.dialog_desc") }}</DialogDescription>
        </DialogHeader>
        <div class="flex flex-col gap-3">
          <label class="flex flex-col gap-1">
            <span class="text-sm text-[var(--text-2)]">{{ t("role.name_label") }}</span>
            <Input
              v-model="form.name"
              :disabled="editingName !== null"
              placeholder="auditor"
            />
          </label>
          <label class="flex flex-col gap-1">
            <span class="text-sm text-[var(--text-2)]">{{ t("role.label_label") }}</span>
            <Input v-model="form.label" :placeholder="t('role.label_ph')" />
          </label>
          <div class="flex flex-col gap-1.5">
            <span class="text-sm text-[var(--text-2)]">{{ t("role.perms_label") }}</span>
            <label
              v-for="p in permissions"
              :key="p"
              class="flex cursor-pointer items-start gap-2 text-sm"
            >
              <input
                type="checkbox"
                class="mt-0.5 accent-[var(--accent)]"
                :checked="form.permissions.includes(p)"
                @change="togglePerm(p)"
              />
              <span>
                {{ t(`role.perm.${p}`) }}
                <span class="block text-xs text-[var(--text-3)]">{{ t(`role.perm_desc.${p}`) }}</span>
              </span>
            </label>
          </div>
        </div>
        <DialogFooter>
          <Button variant="outline" @click="dialogOpen = false">{{ t("common.cancel") }}</Button>
          <Button :disabled="saving || !form.name.trim()" @click="submit">
            {{ t("common.save") }}
          </Button>
        </DialogFooter>
      </DialogContent>
    </Dialog>
  </article>
</template>
