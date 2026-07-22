<script setup lang="ts">
// 新建 API Key（一次性展示完整 key）/ 吊销确认两个 Dialog，从 ApiKeyCard 拆出
// （>200 行拆分约定）。用 v-model 双向绑定父组件的 createOpen/revokeTarget，
// 成功后 emit changed 让父组件重新拉取列表。
import { reactive, ref, watch } from "vue";
import { useI18n } from "vue-i18n";
import { toast } from "vue-sonner";
import { Copy } from "@lucide/vue";
import { Button } from "@/components/ui/button";
import { Input } from "@/components/ui/input";
import {
  Select, SelectContent, SelectGroup, SelectItem, SelectTrigger, SelectValue,
} from "@/components/ui/select";
import {
  Dialog, DialogContent, DialogHeader, DialogTitle, DialogDescription, DialogFooter,
} from "@/components/ui/dialog";
import { createApiKey, revokeApiKey, type ApiKeyItem } from "@/lib/api";

const { t } = useI18n();

const ROLES = ["admin", "editor", "viewer"] as const;

const props = defineProps<{ createOpen: boolean; revokeTarget: ApiKeyItem | null }>();
const emit = defineEmits<{
  "update:createOpen": [value: boolean];
  "update:revokeTarget": [value: ApiKeyItem | null];
  changed: [];
}>();

// ---- 新建 ----
const creating = ref(false);
const newKey = reactive({ name: "", role: "viewer" as string });
const createdFullKey = ref<string | null>(null);

watch(() => props.createOpen, (isOpen) => {
  if (!isOpen) return;
  newKey.name = "";
  newKey.role = "viewer";
  createdFullKey.value = null;
});

async function submitCreate() {
  if (!newKey.name.trim()) return;
  creating.value = true;
  try {
    const r = await createApiKey({ name: newKey.name.trim(), role: newKey.role });
    createdFullKey.value = r.key; // 弹窗保持打开，一次性展示完整 key
    emit("changed");
  } catch (err) {
    toast.error(err instanceof Error ? err.message : String(err));
  } finally {
    creating.value = false;
  }
}

async function copyKey() {
  if (!createdFullKey.value) return;
  try {
    await navigator.clipboard.writeText(createdFullKey.value);
    toast.success(t("msg.copied"));
  } catch {
    toast.error(t("apikey.copy_failed"));
  }
}

function closeCreateDialog() {
  emit("update:createOpen", false);
  createdFullKey.value = null;
}

// ---- 吊销 ----
const revoking = ref(false);

async function confirmRevoke() {
  if (!props.revokeTarget) return;
  revoking.value = true;
  try {
    await revokeApiKey(props.revokeTarget.id);
    toast.success(t("apikey.revoked_toast", { name: props.revokeTarget.name }));
    emit("update:revokeTarget", null);
    emit("changed");
  } catch (err) {
    toast.error(err instanceof Error ? err.message : String(err));
  } finally {
    revoking.value = false;
  }
}
</script>

<template>
  <!-- 新建 Key Dialog：创建前是表单，创建后切换成一次性展示完整 key -->
  <Dialog :open="createOpen" @update:open="(v) => { if (!v) closeCreateDialog(); }">
    <DialogContent>
      <template v-if="!createdFullKey">
        <DialogHeader>
          <DialogTitle>{{ t("apikey.create_title") }}</DialogTitle>
          <DialogDescription>{{ t("apikey.create_desc") }}</DialogDescription>
        </DialogHeader>
        <div class="flex flex-col gap-3">
          <label class="flex flex-col gap-1">
            <span class="text-sm text-[var(--text-2)]">{{ t("common.name") }}</span>
            <Input v-model="newKey.name" :placeholder="t('apikey.name_ph')" />
          </label>
          <label class="flex flex-col gap-1">
            <span class="text-sm text-[var(--text-2)]">{{ t("common.role_col") }}</span>
            <Select v-model="newKey.role">
              <SelectTrigger><SelectValue /></SelectTrigger>
              <SelectContent>
                <SelectGroup>
                  <SelectItem v-for="r in ROLES" :key="r" :value="r">{{ t(`common.role.${r}`) }}</SelectItem>
                </SelectGroup>
              </SelectContent>
            </Select>
          </label>
        </div>
        <DialogFooter>
          <Button variant="outline" @click="closeCreateDialog">{{ t("common.cancel") }}</Button>
          <Button :disabled="creating || !newKey.name.trim()" @click="submitCreate">{{ t("common.create") }}</Button>
        </DialogFooter>
      </template>
      <template v-else>
        <DialogHeader>
          <DialogTitle>{{ t("apikey.save_now") }}</DialogTitle>
          <DialogDescription>{{ t("apikey.save_now_desc") }}</DialogDescription>
        </DialogHeader>
        <div class="flex items-center gap-2 rounded-[var(--radius-ctl)] border border-[var(--border)] bg-[var(--surface-2)] p-3">
          <code class="flex-1 truncate font-mono text-sm">{{ createdFullKey }}</code>
          <Button variant="ghost" size="icon-sm" :aria-label="t('msg.copy')" @click="copyKey">
            <Copy class="size-3.5" />
          </Button>
        </div>
        <DialogFooter>
          <Button @click="closeCreateDialog">{{ t("apikey.saved_close") }}</Button>
        </DialogFooter>
      </template>
    </DialogContent>
  </Dialog>

  <!-- 吊销确认 Dialog -->
  <Dialog :open="!!revokeTarget" @update:open="(v) => { if (!v) emit('update:revokeTarget', null); }">
    <DialogContent>
      <DialogHeader>
        <DialogTitle>{{ t("apikey.revoke_title") }}</DialogTitle>
        <DialogDescription>
          {{ t("apikey.revoke_confirm", { name: revokeTarget?.name }) }}
        </DialogDescription>
      </DialogHeader>
      <DialogFooter>
        <Button variant="outline" @click="emit('update:revokeTarget', null)">{{ t("common.cancel") }}</Button>
        <Button variant="destructive" :disabled="revoking" @click="confirmRevoke">{{ t("apikey.confirm_revoke") }}</Button>
      </DialogFooter>
    </DialogContent>
  </Dialog>
</template>
