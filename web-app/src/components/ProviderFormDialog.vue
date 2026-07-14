<script setup lang="ts">
// 添加/编辑 Provider Dialog。provider 为 null 时是添加模式（POST），非空为
// 编辑模式（name 只读，PUT）。params 文本域客户端 JSON 校验
// （validateParamsJson），非法时 textarea 下方内联报错并阻止提交。成功后
// emit saved(mode, name)：父组件刷新列表；编辑模式父组件还需作废该
// provider 的旧测试徽章（配置已变，历史测试结果失效）。
// M5-2：新增主流厂商预设下拉（添加模式，选中即预填 base_url/model/
// api_key_env，均可再改）与 API Key 直配输入（密钥存服务端 DB，编辑时
// 留空=不修改，勾选"清除"回退环境变量）。
// 模型选择升级：填好 base_url+Key 后点"获取模型列表"（服务端代理拉
// {base_url}/models 并缓存 7 天，过期由访问自动周更），model 输入框变成
// datalist 下拉可选+仍可自由输入——同样适用于企业内部自有 OpenAI 兼容
// 平台（自定义 base_url 即可）。
import { computed, reactive, ref, watch } from "vue";
import { toast } from "vue-sonner";
import { CheckCircle2, RefreshCw, Loader2 } from "@lucide/vue";
import { Button } from "@/components/ui/button";
import { Input } from "@/components/ui/input";
import {
  Dialog, DialogContent, DialogHeader, DialogTitle, DialogDescription, DialogFooter,
} from "@/components/ui/dialog";
import {
  Select, SelectContent, SelectItem, SelectTrigger, SelectValue,
} from "@/components/ui/select";
import {
  createProvider, listModelCatalogs, refreshModelCatalog, updateProvider,
  type ModelCatalog, type Provider,
} from "@/lib/api";
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

// ---- 模型目录：base_url → 已缓存的模型清单 ----
const catalogs = ref<Map<string, ModelCatalog>>(new Map());
const fetchingModels = ref(false);
const modelsError = ref<string | null>(null);

const currentCatalog = computed(() =>
  catalogs.value.get(form.base_url.trim().replace(/\/+$/, "")) ?? null);

async function loadCatalogs() {
  try {
    const { catalogs: list } = await listModelCatalogs();
    catalogs.value = new Map(list.map((c) => [c.base_url, c]));
  } catch {
    // 目录只是辅助，拉不到不阻塞表单（model 仍可手输）
  }
}

async function fetchModels() {
  const baseUrl = form.base_url.trim();
  if (!baseUrl) {
    modelsError.value = "请先填写 base_url";
    return;
  }
  fetchingModels.value = true;
  modelsError.value = null;
  try {
    // 编辑模式且没改 key：用已存 provider 的凭据；否则用表单里的 key/env
    const body = (props.provider && !form.api_key.trim())
      ? { provider_name: props.provider.name }
      : { base_url: baseUrl, api_key: form.api_key.trim() || undefined,
          api_key_env: form.api_key_env.trim() || undefined };
    const catalog = await refreshModelCatalog(body);
    catalogs.value.set(catalog.base_url, catalog);
    catalogs.value = new Map(catalogs.value);   // 触发响应式更新
    toast.success(`已获取 ${catalog.models.length} 个模型`);
  } catch (err) {
    modelsError.value = err instanceof Error ? err.message : String(err);
  } finally {
    fetchingModels.value = false;
  }
}

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
  modelsError.value = null;
  loadCatalogs();     // 已缓存的各端点模型清单（GET 同时触发服务端周更）
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
          <div class="flex items-center justify-between">
            <span class="text-sm text-[var(--text-2)]">model</span>
            <!-- 服务端代理拉 {base_url}/models（需先填 base_url 与密钥）；
                 结果缓存 7 天，过期后台自动周更。企业自有 OpenAI 兼容平台
                 同样适用——自定义 base_url 即可 -->
            <Button
              variant="ghost" size="sm" type="button"
              :disabled="fetchingModels"
              @click="fetchModels"
            >
              <Loader2 v-if="fetchingModels" class="size-3.5 animate-spin" />
              <RefreshCw v-else class="size-3.5" />
              获取模型列表
            </Button>
          </div>
          <!-- datalist：有清单时下拉可选，同时永远支持自由输入（自定义模型名） -->
          <Input v-model="form.model" list="provider-model-options" placeholder="点上方按钮获取列表，或直接输入模型名" />
          <datalist id="provider-model-options">
            <option v-for="m in currentCatalog?.models ?? []" :key="m" :value="m" />
          </datalist>
          <span v-if="modelsError" class="text-xs text-[var(--err)]">{{ modelsError }}</span>
          <span v-else-if="currentCatalog" class="text-xs text-[var(--text-3)]">
            {{ currentCatalog.models.length }} 个模型
            · 更新于 {{ currentCatalog.fetched_at?.slice(0, 16).replace("T", " ") ?? "未知" }}
            {{ currentCatalog.stale ? "（已过期，将自动刷新）" : "" }}
          </span>
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
