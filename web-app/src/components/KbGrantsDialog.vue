<script setup lang="ts">
// 库级权限对话框（M6-3）：勾选哪些用户可访问该库。
// 语义：不勾任何人=公开（所有登录用户可见）；勾了=仅勾选用户+建库人+admin
// 可见。全量覆盖保存。
import { ref, watch, computed } from "vue";
import { useI18n } from "vue-i18n";
import { toast } from "vue-sonner";
import { Button } from "@/components/ui/button";
import {
  Dialog, DialogContent, DialogHeader, DialogTitle, DialogDescription, DialogFooter,
} from "@/components/ui/dialog";
import {
  getKbGrants, putKbGrants, listUsers, type Kb, type UserItem,
} from "@/lib/api";

const props = defineProps<{ open: boolean; kb: Kb | null }>();
const emit = defineEmits<{ "update:open": [value: boolean] }>();
const { t } = useI18n();

const users = ref<UserItem[]>([]);
const selected = ref<Set<string>>(new Set());
const loading = ref(false);
const saving = ref(false);

const isPublic = computed(() => selected.value.size === 0);

watch(() => props.open, async (isOpen) => {
  if (!isOpen || !props.kb) return;
  loading.value = true;
  try {
    const [us, gs] = await Promise.all([listUsers(), getKbGrants(props.kb.id)]);
    users.value = us;
    selected.value = new Set(gs.grants.map((g) => g.user_id));
  } catch (err) {
    toast.error(err instanceof Error ? err.message : String(err));
  } finally {
    loading.value = false;
  }
});

function toggle(id: string) {
  const next = new Set(selected.value);
  if (next.has(id)) next.delete(id);
  else next.add(id);
  selected.value = next;
}

async function save() {
  if (!props.kb) return;
  saving.value = true;
  try {
    await putKbGrants(props.kb.id, [...selected.value]);
    toast.success(isPublic.value ? t("grants.saved_public")
      : t("grants.saved_limited", { count: selected.value.size }));
    emit("update:open", false);
  } catch (err) {
    toast.error(err instanceof Error ? err.message : String(err));
  } finally {
    saving.value = false;
  }
}
</script>

<template>
  <Dialog :open="open" @update:open="(v) => emit('update:open', v)">
    <DialogContent class="max-h-[85vh] overflow-hidden">
      <DialogHeader>
        <DialogTitle>{{ t("grants.title", { name: kb?.name }) }}</DialogTitle>
        <DialogDescription>
          {{ t("grants.desc") }}
        </DialogDescription>
      </DialogHeader>

      <div class="rounded-[var(--radius-ctl)] px-3 py-2 text-sm"
           :class="isPublic ? 'bg-[var(--ok-weak)] text-[var(--ok)]' : 'bg-[var(--accent-weak)] text-[var(--accent-text)]'">
        {{ isPublic ? t("grants.current_public") : t("grants.current_limited", { count: selected.size }) }}
      </div>

      <div class="max-h-[50vh] overflow-y-auto">
        <p v-if="loading" class="py-4 text-center text-sm text-[var(--text-3)]">{{ t("common.loading") }}</p>
        <p v-else-if="!users.length" class="py-4 text-center text-sm text-[var(--text-3)]">
          {{ t("grants.no_users") }}
        </p>
        <label
          v-for="u in users"
          v-else
          :key="u.id"
          class="flex cursor-pointer items-center gap-3 border-b border-[var(--border)] py-2"
        >
          <input
            type="checkbox"
            class="accent-[var(--accent)]"
            :checked="selected.has(u.id)"
            @change="toggle(u.id)"
          />
          <span class="text-sm">{{ u.username }}</span>
          <span class="ml-auto text-xs text-[var(--text-3)]">{{ u.role }}</span>
        </label>
      </div>

      <DialogFooter>
        <Button variant="outline" @click="emit('update:open', false)">{{ t("common.cancel") }}</Button>
        <Button :disabled="saving || loading" @click="save">{{ t("common.save") }}</Button>
      </DialogFooter>
    </DialogContent>
  </Dialog>
</template>
