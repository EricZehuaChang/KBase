<script setup lang="ts">
// 新建 API Key（一次性展示完整 key）/ 吊销确认两个 Dialog，从 ApiKeyCard 拆出
// （>200 行拆分约定）。用 v-model 双向绑定父组件的 createOpen/revokeTarget，
// 成功后 emit changed 让父组件重新拉取列表。
import { reactive, ref, watch } from "vue";
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
    toast.success("已复制到剪贴板");
  } catch {
    toast.error("复制失败，请手动选中复制");
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
    toast.success(`已吊销: ${props.revokeTarget.name}`);
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
          <DialogTitle>新建 API Key</DialogTitle>
          <DialogDescription>设置名称与角色，完整 key 仅在创建后展示一次</DialogDescription>
        </DialogHeader>
        <div class="flex flex-col gap-3">
          <label class="flex flex-col gap-1">
            <span class="text-sm text-[var(--text-2)]">名称</span>
            <Input v-model="newKey.name" placeholder="如 mcp-server" />
          </label>
          <label class="flex flex-col gap-1">
            <span class="text-sm text-[var(--text-2)]">角色</span>
            <Select v-model="newKey.role">
              <SelectTrigger><SelectValue /></SelectTrigger>
              <SelectContent>
                <SelectGroup>
                  <SelectItem v-for="r in ROLES" :key="r" :value="r">{{ r }}</SelectItem>
                </SelectGroup>
              </SelectContent>
            </Select>
          </label>
        </div>
        <DialogFooter>
          <Button variant="outline" @click="closeCreateDialog">取消</Button>
          <Button :disabled="creating || !newKey.name.trim()" @click="submitCreate">创建</Button>
        </DialogFooter>
      </template>
      <template v-else>
        <DialogHeader>
          <DialogTitle>请立即保存此 Key</DialogTitle>
          <DialogDescription>关闭本弹窗后将无法再次查看完整 key</DialogDescription>
        </DialogHeader>
        <div class="flex items-center gap-2 rounded-[var(--radius-ctl)] border border-[var(--border)] bg-[var(--surface-2)] p-3">
          <code class="flex-1 truncate font-mono text-sm">{{ createdFullKey }}</code>
          <Button variant="ghost" size="icon-sm" aria-label="复制" @click="copyKey">
            <Copy class="size-3.5" />
          </Button>
        </div>
        <DialogFooter>
          <Button @click="closeCreateDialog">我已保存，关闭</Button>
        </DialogFooter>
      </template>
    </DialogContent>
  </Dialog>

  <!-- 吊销确认 Dialog -->
  <Dialog :open="!!revokeTarget" @update:open="(v) => { if (!v) emit('update:revokeTarget', null); }">
    <DialogContent>
      <DialogHeader>
        <DialogTitle>吊销 API Key</DialogTitle>
        <DialogDescription>
          确认吊销「{{ revokeTarget?.name }}」？吊销后使用该 key 的请求将立即被拒绝，此操作不可撤销。
        </DialogDescription>
      </DialogHeader>
      <DialogFooter>
        <Button variant="outline" @click="emit('update:revokeTarget', null)">取消</Button>
        <Button variant="destructive" :disabled="revoking" @click="confirmRevoke">确认吊销</Button>
      </DialogFooter>
    </DialogContent>
  </Dialog>
</template>
