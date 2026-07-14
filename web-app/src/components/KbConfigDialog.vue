<script setup lang="ts">
// KB 配置 Dialog：chunk_size/chunk_overlap 数字输入 + enrich Switch。
// 打开时按 props.kb.config 回填（无配置则用后端默认值 512/64/关闭，见
// kbase/plugins/chunkers/structure.py StructureChunker 构造函数默认值）；保存调用
// putKbConfig 并在成功后 emit saved，父组件负责刷新 KB 列表。
import { ref, watch } from "vue";
import { toast } from "vue-sonner";
import { Button } from "@/components/ui/button";
import { Input } from "@/components/ui/input";
import { Switch } from "@/components/ui/switch";
import {
  Dialog, DialogContent, DialogHeader, DialogTitle, DialogDescription, DialogFooter,
} from "@/components/ui/dialog";
import { putKbConfig, type Kb } from "@/lib/api";

const props = defineProps<{ open: boolean; kb: Kb | null }>();
const emit = defineEmits<{ "update:open": [value: boolean]; saved: [] }>();

const chunkSize = ref(512);
const chunkOverlap = ref(64);
const enrichEnabled = ref(false);
const saving = ref(false);

// 每次打开都按当前 kb.config 重新回填，避免残留上一次编辑的未保存值
watch(() => props.open, (isOpen) => {
  if (!isOpen) return;
  const cfg = props.kb?.config;
  chunkSize.value = cfg?.chunk_size ?? 512;
  chunkOverlap.value = cfg?.chunk_overlap ?? 64;
  enrichEnabled.value = cfg?.enrich?.enabled ?? false;
});

async function save() {
  if (!props.kb) return;
  saving.value = true;
  try {
    await putKbConfig(props.kb.id, {
      chunk_size: chunkSize.value,
      chunk_overlap: chunkOverlap.value,
      enrich: { enabled: enrichEnabled.value },
    });
    toast.success("配置已保存");
    emit("update:open", false);
    emit("saved");
  } catch (err) {
    toast.error(err instanceof Error ? err.message : String(err));
  } finally {
    saving.value = false;
  }
}
</script>

<template>
  <Dialog :open="open" @update:open="(v) => emit('update:open', v)">
    <DialogContent>
      <DialogHeader>
        <DialogTitle>知识库配置</DialogTitle>
        <DialogDescription>分块大小与上下文增强设置，仅影响后续新上传的文档</DialogDescription>
      </DialogHeader>
      <div class="flex flex-col gap-4">
        <!-- 向量模型绑定只读展示（M5-2）：建库时定死，改绑=全库向量作废，
             故这里不提供修改入口，仅让管理员看清当前库用的是哪个模型 -->
        <div class="flex items-center justify-between text-sm">
          <span class="text-[var(--text-2)]">向量模型（建库时绑定，不可更改）</span>
          <span class="text-[var(--text-3)]">{{ kb?.config?.embedder ?? "默认" }}</span>
        </div>
        <label class="flex flex-col gap-1">
          <span class="text-sm text-[var(--text-2)]">分块大小 chunk_size（64-4096）</span>
          <Input v-model.number="chunkSize" type="number" min="64" max="4096" />
        </label>
        <label class="flex flex-col gap-1">
          <span class="text-sm text-[var(--text-2)]">分块重叠 chunk_overlap（0-512，需小于分块大小）</span>
          <Input v-model.number="chunkOverlap" type="number" min="0" max="512" />
        </label>
        <label class="flex items-center justify-between">
          <span class="text-sm text-[var(--text-2)]">上下文增强 enrich</span>
          <Switch v-model="enrichEnabled" />
        </label>
      </div>
      <DialogFooter>
        <Button variant="outline" @click="emit('update:open', false)">取消</Button>
        <Button :disabled="saving" @click="save">保存</Button>
      </DialogFooter>
    </DialogContent>
  </Dialog>
</template>
