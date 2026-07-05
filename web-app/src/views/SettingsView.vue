<script setup lang="ts">
// 设置页编排：Provider 卡片网格（ProviderCard）+ 添加/编辑 Dialog
// （ProviderFormDialog）+ 删除确认 + 系统状态面板（healthz）+ 主题 Segmented。
// 测试状态按 provider 名集中持有（testStates），编辑保存后作废对应徽章。
import { onMounted, reactive, ref } from "vue";
import { toast } from "vue-sonner";
import { Plus } from "@lucide/vue";
import { Button } from "@/components/ui/button";
import {
  Dialog, DialogContent, DialogHeader, DialogTitle, DialogDescription, DialogFooter,
} from "@/components/ui/dialog";
import ProviderCard from "@/components/ProviderCard.vue";
import ProviderFormDialog from "@/components/ProviderFormDialog.vue";
import UserManagementCard from "@/components/UserManagementCard.vue";
import ApiKeyCard from "@/components/ApiKeyCard.vue";
import LicenseCard from "@/components/LicenseCard.vue";
import {
  settingsListProviders, deleteProvider, setActiveProvider, testProvider, healthz,
  currentRole,
  type Provider, type HealthzResponse,
} from "@/lib/api";
import { healthDot, type ProviderTestState } from "@/lib/settings-utils";
import { canAdminister } from "@/lib/auth-utils";
import { theme, setTheme } from "@/lib/theme";

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

const testStates = reactive<Record<string, ProviderTestState>>({});

async function handleTest(name: string) {
  testStates[name] = { status: "testing" };
  try {
    const r = await testProvider(name);
    testStates[name] = r.ok
      ? { status: "ok", latencyMs: r.latency_ms }
      : { status: "fail", error: r.error ?? "未知错误" };
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

const dialogOpen = ref(false);
const editTarget = ref<Provider | null>(null);

function openCreateDialog() {
  editTarget.value = null;
  dialogOpen.value = true;
}

function openEditDialog(p: Provider) {
  editTarget.value = p;
  dialogOpen.value = true;
}

function handleSaved(mode: "create" | "edit", name: string) {
  // 编辑成功后作废旧测试徽章——它对应修改前的配置，展示会误导
  if (mode === "edit") delete testStates[name];
  loadProviders();
}

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

    <!-- Provider 卡片列表 -->
    <section class="mt-4">
      <h2 class="mb-3 text-sm font-medium text-[var(--text-2)]">模型 Provider</h2>
      <p v-if="loading" class="text-sm text-[var(--text-3)]">加载中…</p>
      <p v-else-if="!providers.length" class="text-sm text-[var(--text-3)]">
        暂无 provider，请先添加一个
      </p>
      <div v-else class="grid grid-cols-[repeat(auto-fill,minmax(280px,1fr))] gap-4">
        <ProviderCard
          v-for="p in providers"
          :key="p.name"
          :provider="p"
          :is-active="p.name === active"
          :test-state="testStates[p.name]"
          @set-active="handleSetActive(p.name)"
          @edit="openEditDialog(p)"
          @delete="deleteTarget = p"
          @test="handleTest(p.name)"
        />
      </div>
    </section>

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

    <!-- 管理员专属：用户管理 / API Key / 许可证状态。后端已用 require_admin
         强制校验，这里的 v-if 只是 UX 防呆——非 admin 理论上进不到这个路由
         （AppShell 隐藏了设置入口），双重防御。 -->
    <template v-if="canAdminister(currentRole ?? '')">
      <section class="mt-8">
        <h2 class="mb-3 text-sm font-medium text-[var(--text-2)]">管理</h2>
        <div class="flex flex-col gap-4">
          <UserManagementCard />
          <ApiKeyCard />
          <LicenseCard />
        </div>
      </section>
    </template>

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

  <ProviderFormDialog v-model:open="dialogOpen" :provider="editTarget" @saved="handleSaved" />

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
