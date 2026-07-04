<script setup lang="ts">
// 设置页：Provider 卡片管理（设为默认/编辑/删除/连通测试）+ 添加 Provider Dialog +
// 系统状态面板（healthz）+ 主题 Segmented 控件。
import { onMounted, reactive, ref } from "vue";
import { toast } from "vue-sonner";
import { CheckCircle2, Loader2, Pencil, Plus, Trash2 } from "@lucide/vue";
import { Button } from "@/components/ui/button";
import { Input } from "@/components/ui/input";
import { Badge } from "@/components/ui/badge";
import { Tooltip, TooltipContent, TooltipTrigger, TooltipProvider } from "@/components/ui/tooltip";
import {
  Dialog, DialogContent, DialogHeader, DialogTitle, DialogDescription, DialogFooter,
} from "@/components/ui/dialog";
import {
  settingsListProviders, createProvider, updateProvider, deleteProvider,
  setActiveProvider, testProvider, healthz,
  type Provider, type ProviderCreateBody, type HealthzResponse,
} from "@/lib/api";
import { validateParamsJson, paramsSummary, healthDot } from "@/lib/settings-utils";
import { theme, setTheme } from "@/lib/theme";

// ---- Provider 列表 ----
const providers = ref<Provider[]>([]);
const active = ref<string | null>(null);
const loading = ref(true);

async function loadProviders() {
  loading.value = true;
  try {
    const r = await settingsListProviders();
    providers.value = r.providers;
    active.value = r.active;
  } catch (err) {
    toast.error(err instanceof Error ? err.message : String(err));
  } finally {
    loading.value = false;
  }
}

// ---- 测试连通性：每个 provider 名字对应独立的进行中/结果状态 ----
type TestState = { status: "idle" | "testing" | "ok" | "fail"; latencyMs?: number; error?: string };
const testStates = reactive<Record<string, TestState>>({});

async function handleTest(name: string) {
  testStates[name] = { status: "testing" };
  try {
    const r = await testProvider(name);
    if (r.ok) {
      testStates[name] = { status: "ok", latencyMs: r.latency_ms };
    } else {
      testStates[name] = { status: "fail", error: r.error ?? "未知错误" };
    }
  } catch (err) {
    testStates[name] = { status: "fail", error: err instanceof Error ? err.message : String(err) };
  }
}

async function handleSetActive(name: string) {
  try {
    await setActiveProvider(name);
    toast.success(`已设为默认: ${name}`);
    await loadProviders();
  } catch (err) {
    toast.error(err instanceof Error ? err.message : String(err));
  }
}

// ---- 删除确认 ----
const deleteTarget = ref<Provider | null>(null);

async function confirmDelete() {
  if (!deleteTarget.value) return;
  const name = deleteTarget.value.name;
  try {
    await deleteProvider(name);
    toast.success(`已删除: ${name}`);
  } catch (err) {
    toast.error(err instanceof Error ? err.message : String(err));
  } finally {
    deleteTarget.value = null;
    await loadProviders();
  }
}

// ---- 添加/编辑 Dialog（共用表单状态；editingName 非空即编辑模式） ----
const dialogOpen = ref(false);
const editingName = ref<string | null>(null);
const saving = ref(false);

const form = reactive({
  name: "",
  base_url: "",
  api_key_env: "",
  model: "",
  max_concurrency: 1,
  paramsText: "",
});
const paramsError = ref<string | null>(null);

function openCreateDialog() {
  editingName.value = null;
  form.name = "";
  form.base_url = "";
  form.api_key_env = "";
  form.model = "";
  form.max_concurrency = 1;
  form.paramsText = "";
  paramsError.value = null;
  dialogOpen.value = true;
}

function openEditDialog(p: Provider) {
  editingName.value = p.name;
  form.name = p.name;
  form.base_url = p.base_url;
  form.api_key_env = p.api_key_env;
  form.model = p.model;
  form.max_concurrency = p.max_concurrency;
  form.paramsText = Object.keys(p.params ?? {}).length ? JSON.stringify(p.params, null, 2) : "";
  paramsError.value = null;
  dialogOpen.value = true;
}

function validateParams(): Record<string, unknown> | null {
  const r = validateParamsJson(form.paramsText);
  if (!r.ok) {
    paramsError.value = r.error;
    return null;
  }
  paramsError.value = null;
  return r.value;
}

async function submitDialog() {
  const params = validateParams();
  if (params === null) return;
  if (!form.base_url.trim() || !form.model.trim() || !form.api_key_env.trim()) return;
  if (!editingName.value && !form.name.trim()) return;

  saving.value = true;
  try {
    if (editingName.value) {
      await updateProvider(editingName.value, {
        base_url: form.base_url.trim(),
        api_key_env: form.api_key_env.trim(),
        model: form.model.trim(),
        max_concurrency: form.max_concurrency,
        params,
      });
      toast.success(`已更新: ${editingName.value}`);
    } else {
      const body: ProviderCreateBody = {
        name: form.name.trim(),
        base_url: form.base_url.trim(),
        api_key_env: form.api_key_env.trim(),
        model: form.model.trim(),
        max_concurrency: form.max_concurrency,
        params,
      };
      await createProvider(body);
      toast.success(`已添加: ${body.name}`);
    }
    dialogOpen.value = false;
    await loadProviders();
  } catch (err) {
    toast.error(err instanceof Error ? err.message : String(err));
  } finally {
    saving.value = false;
  }
}

// ---- 系统状态面板 ----
const health = ref<HealthzResponse | null>(null);
const healthError = ref<string | null>(null);

async function loadHealth() {
  try {
    health.value = await healthz();
    healthError.value = null;
  } catch (err) {
    healthError.value = err instanceof Error ? err.message : String(err);
  }
}

onMounted(async () => {
  await Promise.all([loadProviders(), loadHealth()]);
});
</script>

<template>
  <div class="p-6">
    <div class="flex items-center justify-between">
      <h1 class="text-lg font-semibold">设置</h1>
      <Button size="sm" @click="openCreateDialog">
        <Plus class="size-3.5" />
        添加 Provider
      </Button>
    </div>

    <TooltipProvider>
      <!-- Provider 卡片列表 -->
      <section class="mt-4">
        <h2 class="mb-3 text-sm font-medium text-[var(--text-2)]">模型 Provider</h2>
        <p v-if="loading" class="text-sm text-[var(--text-3)]">加载中…</p>
        <p v-else-if="!providers.length" class="text-sm text-[var(--text-3)]">
          暂无 provider，请先添加一个
        </p>
        <div v-else class="grid grid-cols-[repeat(auto-fill,minmax(280px,1fr))] gap-4">
          <article
            v-for="p in providers"
            :key="p.name"
            class="rounded-[var(--radius-card)] border bg-[var(--surface)] p-4"
            :class="p.name === active ? 'border-[var(--accent)]' : 'border-[var(--border)]'"
          >
            <div class="flex items-center justify-between gap-2">
              <div class="truncate font-medium">{{ p.name }}</div>
              <Badge v-if="p.name === active" class="bg-[var(--accent-weak)] text-[var(--accent-text)]">
                默认
              </Badge>
            </div>
            <dl class="mt-2 flex flex-col gap-1 text-sm text-[var(--text-2)]">
              <div class="truncate"><dt class="inline text-[var(--text-3)]">模型：</dt>{{ p.model }}</div>
              <div class="truncate"><dt class="inline text-[var(--text-3)]">base_url：</dt>{{ p.base_url }}</div>
              <div><dt class="inline text-[var(--text-3)]">并发：</dt>{{ p.max_concurrency }}</div>
              <div class="truncate" :title="paramsSummary(p.params)">
                <dt class="inline text-[var(--text-3)]">params：</dt>{{ paramsSummary(p.params) }}
              </div>
            </dl>

            <div class="mt-3 flex items-center gap-1.5">
              <Button
                variant="outline" size="sm"
                :disabled="p.name === active"
                @click="handleSetActive(p.name)"
              >
                设为默认
              </Button>
              <Button variant="ghost" size="icon-sm" aria-label="编辑" @click="openEditDialog(p)">
                <Pencil class="size-3.5" />
              </Button>

              <Tooltip v-if="p.name === active">
                <TooltipTrigger as-child>
                  <span>
                    <Button variant="ghost" size="icon-sm" aria-label="删除" disabled>
                      <Trash2 class="size-3.5" />
                    </Button>
                  </span>
                </TooltipTrigger>
                <TooltipContent>默认 provider 不可删除，请先切换默认</TooltipContent>
              </Tooltip>
              <Button
                v-else
                variant="ghost" size="icon-sm" aria-label="删除"
                @click="deleteTarget = p"
              >
                <Trash2 class="size-3.5" />
              </Button>

              <Button
                variant="outline" size="sm"
                class="ml-auto"
                :disabled="testStates[p.name]?.status === 'testing'"
                @click="handleTest(p.name)"
              >
                <Loader2 v-if="testStates[p.name]?.status === 'testing'" class="size-3.5 animate-spin" />
                测试
              </Button>

              <Badge v-if="testStates[p.name]?.status === 'ok'" class="bg-[var(--ok-weak)] text-[var(--ok)]">
                {{ Math.round(testStates[p.name].latencyMs ?? 0) }}ms
              </Badge>
              <Tooltip v-else-if="testStates[p.name]?.status === 'fail'">
                <TooltipTrigger as-child>
                  <Badge class="cursor-default bg-[var(--err-weak)] text-[var(--err)]">失败</Badge>
                </TooltipTrigger>
                <TooltipContent>{{ testStates[p.name].error }}</TooltipContent>
              </Tooltip>
            </div>
          </article>
        </div>
      </section>
    </TooltipProvider>

    <!-- 系统状态面板 -->
    <section class="mt-8">
      <h2 class="mb-3 text-sm font-medium text-[var(--text-2)]">系统状态</h2>
      <div v-if="healthError" class="rounded-[var(--radius-card)] bg-[var(--err-weak)] p-4 text-sm text-[var(--err)]">
        ⚠️ 健康检查失败：{{ healthError }}
      </div>
      <div
        v-else-if="health"
        class="flex flex-wrap gap-6 rounded-[var(--radius-card)] border border-[var(--border)] bg-[var(--surface)] p-4"
      >
        <div v-for="key in (['status', 'embedder', 'vectorstore', 'reranker'] as const)" :key="key" class="flex items-center gap-2">
          <span class="size-2.5 rounded-full" :class="healthDot(health[key]).class" />
          <span class="text-sm text-[var(--text-2)]">
            {{ key }}：<span class="text-[var(--text)]">{{ healthDot(health[key]).label }}</span>
          </span>
        </div>
      </div>
    </section>

    <!-- 主题切换 -->
    <section class="mt-8">
      <h2 class="mb-3 text-sm font-medium text-[var(--text-2)]">外观</h2>
      <div class="inline-flex rounded-[var(--radius-ctl)] border border-[var(--border)] bg-[var(--surface)] p-0.5">
        <button
          type="button"
          class="rounded-[calc(var(--radius-ctl)-2px)] px-3 py-1.5 text-sm transition-colors"
          :class="theme === 'light' ? 'bg-[var(--accent-weak)] text-[var(--accent-text)]' : 'text-[var(--text-2)]'"
          @click="setTheme('light')"
        >
          亮
        </button>
        <button
          type="button"
          class="rounded-[calc(var(--radius-ctl)-2px)] px-3 py-1.5 text-sm transition-colors"
          :class="theme === 'dark' ? 'bg-[var(--accent-weak)] text-[var(--accent-text)]' : 'text-[var(--text-2)]'"
          @click="setTheme('dark')"
        >
          暗
        </button>
      </div>
    </section>
  </div>

  <!-- 添加/编辑 Provider Dialog -->
  <Dialog v-model:open="dialogOpen">
    <DialogContent>
      <DialogHeader>
        <DialogTitle>{{ editingName ? `编辑 Provider：${editingName}` : "添加 Provider" }}</DialogTitle>
        <DialogDescription>
          填写环境变量名而非密钥本身，服务端从环境读取。
        </DialogDescription>
      </DialogHeader>
      <div class="flex flex-col gap-3">
        <label class="flex flex-col gap-1">
          <span class="text-sm text-[var(--text-2)]">name</span>
          <Input v-model="form.name" :disabled="!!editingName" placeholder="provider 唯一标识，如 openai" />
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
        <Button variant="outline" @click="dialogOpen = false">取消</Button>
        <Button :disabled="saving" @click="submitDialog">
          <CheckCircle2 class="size-3.5" />
          {{ editingName ? "保存" : "添加" }}
        </Button>
      </DialogFooter>
    </DialogContent>
  </Dialog>

  <!-- 删除确认 Dialog -->
  <Dialog :open="!!deleteTarget" @update:open="(v) => { if (!v) deleteTarget = null; }">
    <DialogContent>
      <DialogHeader>
        <DialogTitle>删除 Provider</DialogTitle>
        <DialogDescription>
          确认删除「{{ deleteTarget?.name }}」？此操作不可撤销。
        </DialogDescription>
      </DialogHeader>
      <DialogFooter>
        <Button variant="outline" @click="deleteTarget = null">取消</Button>
        <Button variant="destructive" @click="confirmDelete">确认删除</Button>
      </DialogFooter>
    </DialogContent>
  </Dialog>
</template>
