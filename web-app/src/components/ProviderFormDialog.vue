<script setup lang="ts">
// 添加/编辑 Provider Dialog。provider 为 null 时是添加模式（POST），非空为
// 编辑模式（name 只读，PUT）。params 文本域客户端 JSON 校验
// （validateParamsJson），非法时 textarea 下方内联报错并阻止提交。成功后
// emit saved(mode, name)：父组件刷新列表；编辑模式父组件还需作废该
// provider 的旧测试徽章（配置已变，历史测试结果失效）。
import { reactive, ref, watch } from "vue";
import { toast } from "vue-sonner";
import { CheckCircle2 } from "@lucide/vue";
import { Button } from "@/components/ui/button";
import { Input } from "@/components/ui/input";
import {
  Dialog, DialogContent, DialogHeader, DialogTitle, DialogDescription, DialogFooter,
} from "@/components/ui/dialog";
import { createProvider, updateProvider, type Provider } from "@/lib/api";
import { validateParamsJson } from "@/lib/settings-utils";

const props = defineProps<{ open: boolean; provider: Provider | null }>();
const emit = defineEmits<{
  "update:open": [value: boolean];
  saved: [mode: "create" | "edit", name: string];
}>();

const form = reactive({
  name: "",
  base_url: "",
  api_key_env: "",
  model: "",
  max_concurrency: 1,
  paramsText: "",
});
const paramsError = ref<string | null>(null);
const saving = ref(false);

// 每次打开都按 provider 回填（编辑）或清空（添加），避免残留上次编辑内容
watch(() => props.open, (isOpen) => {
  if (!isOpen) return;
  const p = props.provider;
  form.name = p?.name ?? "";
  form.base_url = p?.base_url ?? "";
  form.api_key_env = p?.api_key_env ?? "";
  form.model = p?.model ?? "";
  form.max_concurrency = p?.max_concurrency ?? 1;
  form.paramsText = p && Object.keys(p.params ?? {}).length
    ? JSON.stringify(p.params, null, 2) : "";
  paramsError.value = null;
});

function validateParams(): Record<string, unknown> | null {
  const r = validateParamsJson(form.paramsText);
  if (!r.ok) {
    paramsError.value = r.error;
    return null;
  }
  paramsError.value = null;
  return r.value;
}

async function submit() {
  const params = validateParams();
  if (params === null) return;
  if (!form.base_url.trim() || !form.model.trim() || !form.api_key_env.trim()) return;
  const editing = props.provider !== null;
  if (!editing && !form.name.trim()) return;

  saving.value = true;
  try {
    const body = {
      base_url: form.base_url.trim(),
      api_key_env: form.api_key_env.trim(),
      model: form.model.trim(),
      max_concurrency: form.max_concurrency,
      params,
    };
    const name = editing ? props.provider!.name : form.name.trim();
    if (editing) {
      await updateProvider(name, body);
      toast.success(`已更新: ${name}`);
    } else {
      await createProvider({ name, ...body });
      toast.success(`已添加: ${name}`);
    }
    emit("update:open", false);
    emit("saved", editing ? "edit" : "create", name);
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
        <DialogTitle>{{ provider ? `编辑 Provider：${provider.name}` : "添加 Provider" }}</DialogTitle>
        <DialogDescription>
          填写环境变量名而非密钥本身，服务端从环境读取。
        </DialogDescription>
      </DialogHeader>
      <div class="flex flex-col gap-3">
        <label class="flex flex-col gap-1">
          <span class="text-sm text-[var(--text-2)]">name</span>
          <Input v-model="form.name" :disabled="!!provider" placeholder="provider 唯一标识，如 openai" />
        </label>
        <label class="flex flex-col gap-1">
          <span class="text-sm text-[var(--text-2)]">base_url</span>
          <Input v-model="form.base_url" placeholder="https://api.example.com/v1" />
        </label>
        <label class="flex flex-col gap-1">
          <span class="text-sm text-[var(--text-2)]">api_key_env</span>
          <Input v-model="form.api_key_env" placeholder="OPENAI_API_KEY" />
        </label>
        <label class="flex flex-col gap-1">
          <span class="text-sm text-[var(--text-2)]">model</span>
          <Input v-model="form.model" placeholder="gpt-4o-mini" />
        </label>
        <label class="flex flex-col gap-1">
          <span class="text-sm text-[var(--text-2)]">max_concurrency</span>
          <Input v-model.number="form.max_concurrency" type="number" min="1" />
        </label>
        <label class="flex flex-col gap-1">
          <span class="text-sm text-[var(--text-2)]">params（JSON，可留空）</span>
          <textarea
            v-model="form.paramsText"
            rows="4"
            placeholder='{"temperature":0.7}'
            class="rounded-[var(--radius-ctl)] border border-[var(--border)] bg-[var(--surface)] px-3 py-2 font-mono text-sm text-[var(--text)] outline-none focus:border-[var(--accent)]"
            @blur="validateParams"
          />
          <span v-if="paramsError" class="text-xs text-[var(--err)]">{{ paramsError }}</span>
        </label>
      </div>
      <DialogFooter>
        <Button variant="outline" @click="emit('update:open', false)">取消</Button>
        <Button :disabled="saving" @click="submit">
          <CheckCircle2 class="size-3.5" />
          {{ provider ? "保存" : "添加" }}
        </Button>
      </DialogFooter>
    </DialogContent>
  </Dialog>
</template>
