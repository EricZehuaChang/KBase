<script setup lang="ts">
// 添加/编辑 Provider Dialog。provider 为 null 时是添加模式（POST），非空为
// 编辑模式（name 只读，PUT）。params 文本域客户端 JSON 校验
// （validateParamsJson），非法时 textarea 下方内联报错并阻止提交。成功后
// emit saved(mode, name)：父组件刷新列表；编辑模式父组件还需作废该
// provider 的旧测试徽章（配置已变，历史测试结果失效）。
// M5-2：新增主流厂商预设下拉（添加模式，选中即预填 base_url/model/
// api_key_env，均可再改）与 API Key 直配输入（密钥存服务端 DB，编辑时
// 留空=不修改，勾选"清除"回退环境变量）。
import { computed, reactive, ref, watch } from "vue";
import { toast } from "vue-sonner";
import { CheckCircle2 } from "@lucide/vue";
import { Button } from "@/components/ui/button";
import { Input } from "@/components/ui/input";
import {
  Dialog, DialogContent, DialogHeader, DialogTitle, DialogDescription, DialogFooter,
} from "@/components/ui/dialog";
import {
  Select, SelectContent, SelectItem, SelectTrigger, SelectValue,
} from "@/components/ui/select";
import { createProvider, updateProvider, type Provider } from "@/lib/api";
import {
  buildProviderBody, PROVIDER_PRESETS, validateParamsJson,
} from "@/lib/settings-utils";

const props = defineProps<{ open: boolean; provider: Provider | null }>();
const emit = defineEmits<{
  "update:open": [value: boolean];
  saved: [mode: "create" | "edit", name: string];
}>();

const form = reactive({
  name: "",
  base_url: "",
  api_key_env: "",
  api_key: "",
  model: "",
  max_concurrency: 1,
  paramsText: "",
});
const presetKey = ref("");            // 添加模式的预设选择（"custom"=自定义）
const clearKey = ref(false);          // 编辑模式：显式清除直配密钥
const paramsError = ref<string | null>(null);
const saving = ref(false);

// 每次打开都按 provider 回填（编辑）或清空（添加），避免残留上次编辑内容。
// api_key 输入框始终清空——原文不回显（后端也不返回原文），编辑时留空即不动。
watch(() => props.open, (isOpen) => {
  if (!isOpen) return;
  const p = props.provider;
  form.name = p?.name ?? "";
  form.base_url = p?.base_url ?? "";
  form.api_key_env = p?.api_key_env ?? "";
  form.api_key = "";
  form.model = p?.model ?? "";
  form.max_concurrency = p?.max_concurrency ?? 1;
  form.paramsText = p && Object.keys(p.params ?? {}).length
    ? JSON.stringify(p.params, null, 2) : "";
  presetKey.value = "";
  clearKey.value = false;
  paramsError.value = null;
});

// 选中预设即预填（自定义/未选不动表单）；name 建议用预设 key，仍可改
function applyPreset(key: unknown) {
  const preset = PROVIDER_PRESETS.find((x) => x.key === key);
  if (!preset) return;
  form.base_url = preset.base_url;
  form.model = preset.models[0] ?? "";
  form.api_key_env = preset.api_key_env;
  if (!form.name.trim()) form.name = preset.key;
}

const keyPlaceholder = computed(() => {
  if (props.provider?.has_api_key) {
    return `已配置 ${props.provider.api_key_hint ?? "****"}，留空不修改`;
  }
  return "sk-...（可留空，改用环境变量）";
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
  if (!form.base_url.trim() || !form.model.trim()) return;
  const editing = props.provider !== null;
  if (!editing && !form.name.trim()) return;
  // 密钥来源二选一（后端同样校验）：创建时两个都空直接拦
  if (!editing && !form.api_key.trim() && !form.api_key_env.trim()) {
    toast.error("请填写 API Key 或密钥环境变量名（二选一）");
    return;
  }

  saving.value = true;
  try {
    const body = buildProviderBody(
      { ...form, params }, { editing, clearKey: clearKey.value });
    const name = editing ? props.provider!.name : form.name.trim();
    if (editing) {
      await updateProvider(name, body);
      toast.success(`已更新: ${name}`);
    } else {
      await createProvider({ name, ...body } as Parameters<typeof createProvider>[0]);
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
          API Key 可直接填写（保存在服务端），或填环境变量名由服务端从环境读取。
        </DialogDescription>
      </DialogHeader>
      <div class="flex flex-col gap-3">
        <label v-if="!provider" class="flex flex-col gap-1">
          <span class="text-sm text-[var(--text-2)]">厂商预设</span>
          <Select v-model="presetKey" @update:model-value="applyPreset">
            <SelectTrigger class="w-full">
              <SelectValue placeholder="选择主流厂商自动填写，或手动自定义" />
            </SelectTrigger>
            <SelectContent>
              <SelectItem v-for="p in PROVIDER_PRESETS" :key="p.key" :value="p.key">
                {{ p.label }}
              </SelectItem>
            </SelectContent>
          </Select>
        </label>
        <label class="flex flex-col gap-1">
          <span class="text-sm text-[var(--text-2)]">name</span>
          <Input v-model="form.name" :disabled="!!provider" placeholder="provider 唯一标识，如 openai" />
        </label>
        <label class="flex flex-col gap-1">
          <span class="text-sm text-[var(--text-2)]">base_url</span>
          <Input v-model="form.base_url" placeholder="https://api.example.com/v1" />
        </label>
        <label class="flex flex-col gap-1">
          <span class="text-sm text-[var(--text-2)]">API Key（直接填写，存服务端）</span>
          <Input v-model="form.api_key" type="password" autocomplete="off" :placeholder="keyPlaceholder" />
          <label v-if="provider?.has_api_key" class="flex items-center gap-2 text-xs text-[var(--text-3)]">
            <input v-model="clearKey" type="checkbox" class="accent-[var(--accent)]" />
            清除已保存的 Key（改用下方环境变量）
          </label>
        </label>
        <label class="flex flex-col gap-1">
          <span class="text-sm text-[var(--text-2)]">api_key_env（环境变量名，可选）</span>
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
