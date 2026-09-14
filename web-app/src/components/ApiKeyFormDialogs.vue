<script setup lang="ts">
// 新建 API Key（一次性展示完整 key）/ 吊销确认两个 Dialog，从 ApiKeyCard 拆出
// （>200 行拆分约定）。用 v-model 双向绑定父组件的 createOpen/revokeTarget，
// 成功后 emit changed 让父组件重新拉取列表。
// T09 补：①库白名单选择器——后端 scope_kb_ids 早就支持，此前表单只提交
// 名称+角色（已知缺口，受限 key 只能靠 API/curl 建）；②有效期/配额/IP 白名单
// 三个策略字段（拆到 ApiKeyPolicyFields.vue，与编辑弹窗共用）。
import { reactive, ref, watch } from "vue";
import { useI18n } from "vue-i18n";
import { toast } from "vue-sonner";
import { Copy } from "@lucide/vue";
import { copyToClipboard } from "@/lib/clipboard";
import { Button } from "@/components/ui/button";
import { Input } from "@/components/ui/input";
import {
  Select, SelectContent, SelectGroup, SelectItem, SelectTrigger, SelectValue,
} from "@/components/ui/select";
import {
  Dialog, DialogContent, DialogHeader, DialogTitle, DialogDescription, DialogFooter,
} from "@/components/ui/dialog";
import ApiKeyPolicyFields from "@/components/ApiKeyPolicyFields.vue";
import { buildApiKeyPolicy } from "@/lib/settings-utils";
import { createApiKey, listKbs, revokeApiKey, type ApiKeyItem, type Kb } from "@/lib/api";

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
const newKey = reactive({
  name: "",
  role: "viewer" as string,
  scopeKbIds: [] as string[],
  // 策略字段（空串=不限）：有效期/每分钟/每日/IP 白名单文本
  expiresAt: "",
  rpm: "",
  dailyQuota: "",
  ipAllow: "",
});
const kbs = ref<Kb[]>([]);
const createdFullKey = ref<string | null>(null);

// 库列表按需拉取（打开弹窗时）：API Key 卡片平时不需要库清单
watch(() => props.createOpen, async (isOpen) => {
  if (!isOpen) return;
  newKey.name = "";
  newKey.role = "viewer";
  newKey.scopeKbIds = [];
  newKey.expiresAt = "";
  newKey.rpm = "";
  newKey.dailyQuota = "";
  newKey.ipAllow = "";
  createdFullKey.value = null;
  try {
    kbs.value = await listKbs();
  } catch (err) {
    toast.error(err instanceof Error ? err.message : String(err));
  }
});

function toggleKb(id: string) {
  const next = [...newKey.scopeKbIds];
  const at = next.indexOf(id);
  if (at >= 0) next.splice(at, 1);
  else next.push(id);
  newKey.scopeKbIds = next;
}

async function submitCreate() {
  if (!newKey.name.trim()) return;
  creating.value = true;
  try {
    const r = await createApiKey({
      name: newKey.name.trim(),
      role: newKey.role,
      scope_kb_ids: newKey.scopeKbIds.length ? newKey.scopeKbIds : null,
      ...buildApiKeyPolicy(newKey),
    });
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
  // 兼容工具含 http 非安全上下文回退（execCommand）
  if (await copyToClipboard(createdFullKey.value)) toast.success(t("msg.copied"));
  else toast.error(t("apikey.copy_failed"));
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
    <DialogContent class="max-h-[85vh] overflow-y-auto">
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
          <!-- 库白名单（T09 补齐的已知缺口）：不勾=不限（后端 scope NULL 语义）。
               受限 key 越权查询在服务端静默返回空集，不报错不提示。 -->
          <div class="flex flex-col gap-1">
            <span class="text-sm text-[var(--text-2)]">{{ t("apikey.scope") }}</span>
            <div class="max-h-32 overflow-y-auto rounded-[var(--radius-ctl)] border border-[var(--border)] px-3">
              <p v-if="!kbs.length" class="py-2 text-xs text-[var(--text-3)]">{{ t("apikey.no_kb") }}</p>
              <label v-for="kb in kbs" :key="kb.id" class="flex cursor-pointer items-center gap-2 py-1.5">
                <input
                  type="checkbox"
                  class="accent-[var(--accent)]"
                  :checked="newKey.scopeKbIds.includes(kb.id)"
                  @change="toggleKb(kb.id)"
                />
                <span class="text-sm">{{ kb.name }}</span>
              </label>
            </div>
            <span class="text-xs text-[var(--text-3)]">{{ t("apikey.scope_hint") }}</span>
          </div>
          <ApiKeyPolicyFields
            v-model:expires-at="newKey.expiresAt"
            v-model:rpm="newKey.rpm"
            v-model:daily-quota="newKey.dailyQuota"
            v-model:ip-allow="newKey.ipAllow"
          />
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

  <!-- 吊销确认 Dialog：措辞点明"不可撤销"，并提示临时停用走编辑（T09） -->
  <Dialog :open="!!revokeTarget" @update:open="(v) => { if (!v) emit('update:revokeTarget', null); }">
    <DialogContent>
      <DialogHeader>
        <DialogTitle>{{ t("apikey.revoke_title") }}</DialogTitle>
        <DialogDescription>
          {{ t("apikey.revoke_confirm", { name: revokeTarget?.name }) }}
        </DialogDescription>
      </DialogHeader>
      <!-- 吊销不可恢复；只是要临时断掉调用的话，编辑弹窗里有可恢复的停用开关 -->
      <p class="text-xs text-[var(--text-3)]">{{ t("apikey.revoke_hint") }}</p>
      <DialogFooter>
        <Button variant="outline" @click="emit('update:revokeTarget', null)">{{ t("common.cancel") }}</Button>
        <Button variant="destructive" :disabled="revoking" @click="confirmRevoke">{{ t("apikey.confirm_revoke") }}</Button>
      </DialogFooter>
    </DialogContent>
  </Dialog>
</template>
